#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-11-27
# @Filename: core.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import logging

from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Generic,
    Sequence,
    Type,
    TypeVar,
)

from packaging.version import Version

from clu.client import AMQPReply

from gort.exceptions import GortError
from gort.tools import kubernetes_list_deployments, kubernetes_restart_deployment


if TYPE_CHECKING:
    from gort.gort import Gort


GortDeviceType = TypeVar("GortDeviceType", bound="GortDevice")


class GortDeviceSet(dict[str, GortDeviceType], Generic[GortDeviceType]):
    """A set to gort-managed devices.

    Devices can be accessed as items of the :obj:`.GortDeviceSet` dictionary
    or using dot notation, as attributes.

    Parameters
    ----------
    gort
        The :obj:`.Gort` instance.
    data
        A mapping of device to device info. Each device must at least include
        an ``actor`` key with the actor to use to communicated with the device.
        Any other information is passed to the :obj:`.GortDevice` on instantiation.
    kwargs
        Other keyword arguments to pass wo the device class.

    """

    __DEVICE_CLASS__: ClassVar[Type["GortDevice"]]
    __DEPLOYMENTS__: ClassVar[list[str]] = []

    def __init__(self, gort: Gort, data: dict[str, dict], **kwargs):
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

    async def init(self):
        """Runs asynchronous tasks that must be executed on init."""

        # Run devices init methods.
        results = await asyncio.gather(
            *[dev.init() for dev in self.values()],
            return_exceptions=True,
        )

        for idev, result in enumerate(results):
            if isinstance(result, Exception):
                self.write_to_log(
                    f"Failed initialising device {list(self)[idev]} "
                    f"with error {str(result)}",
                    "error",
                )

        return

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

    async def send_command_all(
        self,
        command: str,
        *args,
        devices: Sequence[str] | None = None,
        **kwargs,
    ):
        """Sends a command to all the devices.

        Parameters
        ----------
        command
            The command to call.
        args, kwargs
            Arguments to pass to the :obj:`.RemoteCommand`.

        """

        tasks = []
        for name, dev in self.items():
            if devices is not None and name not in devices:
                continue

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

    async def restart(self):
        """Restarts the set deployments and resets all controllers.

        Returns
        -------
        result
            A boolean indicting if the entire restart procedure succeeded.

        """

        failed: bool = False

        self.write_to_log("Restarting Kubernetes deployments.", "info")
        for deployment in self.__DEPLOYMENTS__:
            await kubernetes_restart_deployment(deployment)

        self.write_to_log("Waiting 15 seconds for deployments to be ready.", "info")
        await asyncio.sleep(15)

        # Check that deployments are running.
        running_deployments = await kubernetes_list_deployments()
        for deployment in self.__DEPLOYMENTS__:
            if deployment not in running_deployments:
                failed = True
                self.write_to_log(f"Deployment {deployment} did not restart.", "error")

        # Refresh the command models for all the actors.
        await asyncio.gather(*[actor.refresh() for actor in self.gort.actors.values()])

        # Refresh the device set.
        await self.init()

        return not failed


class GortDevice:
    """A gort-managed device.

    Parameters
    ----------
    gort
        The :obj:`.Gort` instance.
    name
        The name of the device.
    actor
        The name of the actor used to interface with this device. The actor is
        added to the list of :obj:`.RemoteActor` in the :obj:`.Gort`.


    """

    def __init__(self, gort: Gort, name: str, actor: str):
        self.gort = gort
        self.name = name
        self.actor = gort.add_actor(actor, device=self)

        # Placeholder version. The real one is retrieved on init.
        self.version = Version("0.99.0")

    async def init(self):
        """Runs asynchronous tasks that must be executed on init.

        If the device is part of a :obj:`.DeviceSet`, this method is called
        by :obj:`.DeviceSet.init`.

        """

        # Get the version of the actor.
        if "version" in self.actor.commands:
            try:
                reply = await self.actor.commands.version()
                if (version := reply.get("version")) is not None:
                    self.version = Version(version)
            except Exception:
                pass

        return

    def write_to_log(
        self,
        message: str,
        level: str = "debug",
        header: str | None = None,
        exc_info: logging._ExcInfoType = None,
        **kwargs,
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

        level_int = logging._nameToLevel.get(level.upper()) or logging.INFO

        self.gort.log.log(level_int, message, exc_info=exc_info, **kwargs)

    def log_replies(self, reply: AMQPReply, skip_debug: bool = True):
        """Outputs command replies."""

        if reply.body:
            if reply.message_code in ["w"]:
                level = "warning"
            elif reply.message_code in ["e", "f", "!"]:
                level = "error"
            else:
                level = "debug"
                if skip_debug:
                    return

            self.write_to_log(str(reply.body), level)
