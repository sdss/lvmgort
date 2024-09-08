#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-02-07
# @Filename: core.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from types import SimpleNamespace

from typing import TYPE_CHECKING, Callable, Literal

import unclick
from aiormq import AMQPConnectionError, ChannelInvalidStateError
from typing_extensions import Self

from clu.tools import CommandStatus

from gort.exceptions import GortTimeoutError, GortWarning, RemoteCommandError

from .tools import get_valid_variable_name


if TYPE_CHECKING:
    from clu.client import AMQPReply
    from clu.command import Command

    from gort.gort import GortDevice

    from .gort import GortClient


__all__ = ["RemoteActor", "RemoteCommand", "ActorReply"]


class CommandSet(dict[str, "RemoteCommand"]):
    """A command set for a remote actor."""

    def __getattribute__(self, __name: str) -> RemoteCommand:
        if __name in self:
            return self[__name]
        return super().__getattribute__(__name)


class RemoteActor:
    """A programmatic representation of a remote actor."""

    def __init__(self, client: GortClient, name: str, device: GortDevice | None = None):
        self.client = client

        self.name = name
        self.model: dict = {}
        self.commands = CommandSet()
        self.device = device

    def __repr__(self):
        return f"<RemoteActor (name={self.name})>"

    async def init(self) -> Self:
        """Initialises the representation of the actor."""

        if (
            self.client.connection.connection
            and self.client.connection.connection.is_closed
        ):
            raise RuntimeError("Gort is not connected.")

        cmd = await self.send_raw_command("get-command-model")
        if cmd.status.did_fail:
            warnings.warn(f"Cannot get model for actor {self.name}.", GortWarning)
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

        # This lock prevents that if the client has disconnected we get multiple
        # reconnections.
        async with self.client._connect_lock:
            try:
                cmd = await self.client.send_command(
                    self.name,
                    *args,
                    await_command=False,
                    **kwargs,
                )
            except (AMQPConnectionError, ChannelInvalidStateError):
                # Client has disconnected. This should only happen if running Gort
                # in an ipython terminal where the event loop only runs while a command
                # is executing. See https://tinyurl.com/4kcwxzx9

                self.client.log.warning("Client has disconnected. Reconnecting.")

                await self.client.start()

                cmd = await self.client.send_command(
                    self.name,
                    *args,
                    await_command=False,
                    **kwargs,
                )

        return await cmd

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

        self._n_retries: int = 1
        self._retry_delay: float = 1

        self.is_group = "commands" in model and len(model["commands"]) > 0
        if self.is_group:
            for command_info in model["commands"].values():
                command_name = get_valid_variable_name(command_info["name"])
                child_command = RemoteCommand(remote_actor, command_info, parent=self)
                setattr(self.commands, command_name, child_command)

    def set_retries(self, n_retries: int, retry_delay: float | None = None):
        """Sets the number of retries and the delay between retries."""

        self._n_retries = n_retries
        self._retry_delay = retry_delay or self._retry_delay

    def get_command_string(self, *args, **kwargs):
        """Gets the command string for a set of arguments."""

        return unclick.build_command_string(self._model, *args, **kwargs)

    async def __call__(
        self,
        *args,
        reply_callback: Callable[[AMQPReply], None] | None | Literal[False] = None,
        timeout: float | None = None,
        _current_retry: int = 1,
        **kwargs,
    ):
        """Executes the remote command with some given arguments."""

        parent_string = ""
        if self._parent is not None:
            # Call parent chain without arguments. This is not bullet-proof for all
            # cases, but probably good enough for now.
            parent_string = self._parent.get_command_string() + " "

        if reply_callback is None and self._remote_actor.device is not None:
            reply_callback = self._remote_actor.device.log_replies

        cmd = await self._remote_actor.send_raw_command(
            parent_string + self.get_command_string(*args, **kwargs),
            callback=reply_callback,
            time_limit=timeout,
        )

        actor_reply = ActorReply(self._remote_actor, cmd)
        for reply in cmd.replies:
            if len(reply.body) > 0:
                actor_reply.replies.append(reply.body)

        actor = self._remote_actor.name
        command_name = self._name

        if cmd.status & CommandStatus.TIMEDOUT:
            raise GortTimeoutError(
                f"Actor {actor!r} timed out executing command {command_name!r}.",
                command=cmd,
                remote_command=self,
            )

        if not cmd.status.did_succeed:
            if _current_retry == self._n_retries:
                raise RemoteCommandError(
                    f"Actor {actor!r} failed executing command {command_name!r}.",
                    command=cmd,
                    remote_command=self,
                )

            error = actor_reply.get("error")
            error = f" {error!s}" if error is not None else ""
            self._remote_actor.client.log.warning(
                f"Actor {actor!r} failed executing command "
                f"{command_name!r}.{error} Retrying.",
            )

            return await self(
                *args,
                reply_callback=reply_callback,
                timeout=timeout,
                _current_retry=_current_retry + 1,
                **kwargs,
            )

        return actor_reply


@dataclass
class ActorReply:
    """A reply to an actor command.

    Attributes
    ----------
    actor
        The :obj:`.RemoteActor` that generated the reply.
    command
        The CLU command instance.
    replies
        A list of replies generated by the command.

    """

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


class LogNamespace:
    """A namespace for log messages."""

    def __init__(self, logger: logging.Logger, header: str = "") -> None:
        self.logger = logger
        self.header = header

    def debug(self, message: str, *args, **kwargs):
        self.logger.log(logging.DEBUG, self._get_message(message), *args, **kwargs)

    def info(self, message: str, *args, **kwargs):
        self.logger.log(logging.INFO, self._get_message(message), *args, **kwargs)

    def warning(self, message: str, *args, **kwargs):
        self.logger.log(logging.WARNING, self._get_message(message), *args, **kwargs)

    def error(self, message: str, *args, **kwargs):
        self.logger.log(logging.ERROR, self._get_message(message), *args, **kwargs)

    def critical(self, message: str, *args, **kwargs):
        self.logger.log(logging.CRITICAL, self._get_message(message), *args, **kwargs)

    def exception(self, *args, **kwargs):
        self.logger.exception(*args, **kwargs)

    def _get_message(self, message: str):
        return self.header.format(**locals()) + message
