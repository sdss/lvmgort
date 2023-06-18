#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-02-07
# @Filename: core.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, Type, TypeVar

import unclick

from trurl.exceptions import TrurlError

from .tools import get_valid_variable_name


if TYPE_CHECKING:
    from clu.command import Command

    from trurl.trurl import Trurl


__all__ = ["RemoteActor", "RemoteCommand", "ActorReply"]


class CommandSet(dict[str, "RemoteCommand"]):
    """A command set for a remote actor."""

    def __getattribute__(self, __name: str) -> RemoteCommand:
        if __name in self:
            return self[__name]
        return super().__getattribute__(__name)


class RemoteActor:
    """A programmatic representation of a remote actor."""

    def __init__(self, trurl: Trurl, name: str):
        self._trurl = trurl

        self.name = name
        self.model: dict = {}
        self.commands = CommandSet()

    def __repr__(self):
        return f"<RemoteActor (name={self.name})>"

    async def init(self) -> Self:
        """Initialises the representation of the actor."""

        if not self._trurl.connected:
            raise RuntimeError("Trurl is not connected.")

        cmd = await self._trurl.client.send_command(self.name, "get-command-model")
        if cmd.status.did_fail:
            raise TrurlError(f"Cannot get model for actor {self.name}.")

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

        return await self._trurl.client.send_command(self.name, *args, **kwargs)

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

        self._is_group = "commands" in model and len(model["commands"]) > 0
        if self._is_group:
            for command_info in model["commands"].values():
                command_name = get_valid_variable_name(command_info["name"])
                child_command = RemoteCommand(remote_actor, command_info, parent=self)
                setattr(self, command_name, child_command)

    def get_command_string(self, *args, **kwargs):
        """Gets the command string for a set of arguments."""

        return unclick.build_command_string(self._model, *args, **kwargs)

    async def __call__(self, *args, **kwargs):
        """Executes the remote command with some given arguments."""

        parent_string = ""
        if self._parent is not None:
            # Call parent chain without arguments. This is not bullet-proof for all
            # cases, but probably good enough for now.
            parent_string = self._parent.get_command_string() + " "

        cmd = await self._remote_actor._trurl.client.send_command(
            self._remote_actor.name,
            parent_string + self.get_command_string(*args, **kwargs),
        )

        actor_reply = ActorReply(self._remote_actor, cmd)
        for reply in cmd.replies:
            if len(reply.body) > 0:
                actor_reply.replies.append(reply.body)

        if not cmd.status.did_succeed:
            error = actor_reply.get("error")
            error = str(error) if error is not None else ""
            raise TrurlError(f"Failed executing command {self._name}. {error}")

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


class TrurlDevice:
    """A trurl-managed device."""

    def __init__(self, trurl: Trurl, name: str, actor: str, **kwargs):
        self.trurl = trurl
        self.name = name
        self.actor = trurl.add_actor(actor)


TrurlDeviceType = TypeVar("TrurlDeviceType", bound=TrurlDevice)


class TrurlDeviceSet(dict[str, TrurlDeviceType], Generic[TrurlDeviceType]):
    """A set to trurl-managed devices."""

    __DEVICE_CLASS__: ClassVar[Type[TrurlDevice]]

    def __init__(self, trurl: Trurl, data: dict[str, dict]):
        self.trurl = trurl

        _dict_data = {}
        for device_name in data:
            device_data = data[device_name].copy()
            actor_name = device_data.pop("actor")
            _dict_data[device_name] = self.__DEVICE_CLASS__(
                trurl,
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
