#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-02-07
# @Filename: core.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass, field
from types import SimpleNamespace

from typing import TYPE_CHECKING, Any, Callable, ClassVar, Generic, Self, Type, TypeVar

import unclick

from sauron.exceptions import SauronError, SauronWarning

from .tools import get_valid_variable_name


if TYPE_CHECKING:
    from clu.client import AMQPReply
    from clu.command import Command

    from sauron import Sauron


__all__ = ["RemoteActor", "RemoteCommand", "ActorReply"]


class CommandSet(dict[str, "RemoteCommand"]):
    """A command set for a remote actor."""

    def __getattribute__(self, __name: str) -> RemoteCommand:
        if __name in self:
            return self[__name]
        return super().__getattribute__(__name)


class RemoteActor:
    """A programmatic representation of a remote actor."""

    def __init__(self, sauron: Sauron, name: str):
        self._sauron = sauron

        self.name = name
        self.model: dict = {}
        self.commands = CommandSet()

    def __repr__(self):
        return f"<RemoteActor (name={self.name})>"

    async def init(self) -> Self:
        """Initialises the representation of the actor."""

        if not self._sauron.connected:
            raise RuntimeError("sauron is not connected.")

        cmd = await self._sauron.client.send_command(self.name, "get-command-model")
        if cmd.status.did_fail:
            warnings.warn(f"Cannot get model for actor {self.name}.", SauronWarning)
            return self

        self.model = cmd.replies.get("command_model")

        commands_dict = {}
        for command_info in self.model["commands"].values():
            command_name = get_valid_variable_name(command_info["name"])
            commands_dict[command_name] = RemoteCommand(self, command_info)

        self.commands = CommandSet(commands_dict)

        return self

    async def send_raw_command(self, *args, **kwargs):
        """Sends a raw command to the actor.

        The parameters are the same as CLU's ``AMQPClient.send_command()`` with
        the exception of the consumer name, which is replaced with the current actor
        name.

        """

        return await self._sauron.client.send_command(self.name, *args, **kwargs)

    async def refresh(self):
        """Refresesh the command list."""

        await self.init()


class RemoteCommand:
    """Representation of a remote command."""

    def __init__(
        self,
        remote_actor: RemoteActor,
        model: dict,
        parent: RemoteCommand | None = None,
    ):
        self._remote_actor = remote_actor
        self._model = model
        self._parent = parent

        self._name = model["name"]
        self.commands = SimpleNamespace()

        self.is_group = "commands" in model and len(model["commands"]) > 0
        if self.is_group:
            for command_info in model["commands"].values():
                command_name = get_valid_variable_name(command_info["name"])
                child_command = RemoteCommand(remote_actor, command_info, parent=self)
                setattr(self.commands, command_name, child_command)

    def get_command_string(self, *args, **kwargs):
        """Gets the command string for a set of arguments."""

        return unclick.build_command_string(self._model, *args, **kwargs)

    async def __call__(
        self,
        *args,
        reply_callback: Callable[[AMQPReply], None] | None = None,
        **kwargs,
    ):
        """Executes the remote command with some given arguments."""

        parent_string = ""
        if self._parent is not None:
            # Call parent chain without arguments. This is not bullet-proof for all
            # cases, but probably good enough for now.
            parent_string = self._parent.get_command_string() + " "

        cmd = await self._remote_actor._sauron.client.send_command(
            self._remote_actor.name,
            parent_string + self.get_command_string(*args, **kwargs),
            callback=reply_callback,
        )

        actor_reply = ActorReply(self._remote_actor, cmd)
        for reply in cmd.replies:
            if len(reply.body) > 0:
                actor_reply.replies.append(reply.body)

        if not cmd.status.did_succeed:
            error = actor_reply.get("error")
            error = str(error) if error is not None else ""
            raise SauronError(f"Failed executing command {self._name}. {error}")

        return actor_reply


@dataclass
class ActorReply:
    """A reply to an actor command."""

    actor: RemoteActor
    command: Command
    replies: list[dict] = field(default_factory=list)

    def flatten(self):
        """Returns a flattened dictionary of replies.

        Note that if a keyword has been output multiple times, only the
        last value is retained.

        """

        result = {}
        for reply in self.replies:
            for key in reply:
                result[key] = reply[key]

        return result

    def get(self, key: str):
        """Returns the first occurrence of a keyword in the reply list."""

        for reply in self.replies:
            if key in reply:
                return reply[key]

        return None


class SauronDevice:
    """A sauron-managed device."""

    def __init__(self, sauron: Sauron, name: str, actor: str, **kwargs):
        self.sauron = sauron
        self.name = name
        self.actor = sauron.add_actor(actor)


sauronDeviceType = TypeVar("sauronDeviceType", bound=SauronDevice)


class SauronDeviceSet(dict[str, sauronDeviceType], Generic[sauronDeviceType]):
    """A set to sauron-managed devices."""

    __DEVICE_CLASS__: ClassVar[Type[SauronDevice]]

    def __init__(self, sauron: Sauron, data: dict[str, dict]):
        self.sauron = sauron

        _dict_data = {}
        for device_name in data:
            device_data = data[device_name].copy()
            actor_name = device_data.pop("actor")
            _dict_data[device_name] = self.__DEVICE_CLASS__(
                sauron,
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
