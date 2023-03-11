#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-02-07
# @Filename: core.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

import unclick
from yaml import warnings

from clu.client import AMQPClient

from lvmbrain.exceptions import LVMBrainUserWarning

from .tools import get_valid_variable_name


if TYPE_CHECKING:
    from typing import Self

    from clu.command import Command


__all__ = ["LVMBrain", "RemoteActor"]


class LVMBrain:
    """The main ``lvmbrain`` client class, used to communicate with the actor system."""

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
                f"lvmbrain-client-{client_uuid}",
                host=host,
                user=user,
                password=password,
            )

    async def init(self) -> Self:
        """Initialises the client."""

        if not self.connected:
            await self.client.start()

        return self

    @property
    def connected(self):
        """Returns `True` if the client is connected."""

        return self.client.connection and self.client.connection.connection is not None


class RemoteActor:
    """A programmatic representation of a remote actor."""

    def __init__(self, brain: LVMBrain, name: str):
        self._brain = brain
        self._name = name

        self._commands: list[str] = []

    async def init(self) -> Self:
        """Initialises the representation of the actor."""

        if not self._brain.connected:
            raise RuntimeError("Brain is not connected.")

        cmd = await self._brain.client.send_command(self._name, "get-command-model")
        if cmd.status.did_fail:
            warnings.warn(
                f"Cannot get model for actor {self._name}.",
                LVMBrainUserWarning,
            )

        model = cmd.replies.get("command_model")

        for command_name in self._commands:
            if hasattr(self, command_name):
                delattr(self, command_name)

        for command_info in model["commands"].values():
            command_name = get_valid_variable_name(command_info["name"])
            setattr(self, command_name, RemoteCommand(self, command_info))
            self._commands.append(command_name)

        return self

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

        cmd = await self._remote_actor._brain.client.send_command(
            self._remote_actor._name,
            parent_string + self.get_command_string(*args, **kwargs),
        )

        actor_reply = ActorReply(cmd, cmd.status.did_succeed)
        for reply in cmd.replies:
            if len(reply.body) > 0:
                actor_reply.replies.append(reply.body)

        return actor_reply


@dataclass
class ActorReply:
    """A reply to an actor command."""

    command: Command
    success: bool
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
