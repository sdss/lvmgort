#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-02-07
# @Filename: remote.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from types import SimpleNamespace

from typing import TYPE_CHECKING, Any, Callable, Literal

import unclick
from aiormq import AMQPConnectionError, ChannelInvalidStateError
from lvmopstools.retrier import Retrier
from typing_extensions import Self

from clu.tools import CommandStatus

from gort.exceptions import (
    GortTimeoutError,
    GortWarning,
    InvalidRemoteCommand,
    RemoteCommandError,
)
from gort.tools import get_valid_variable_name


if TYPE_CHECKING:
    from clu.client import AMQPReply
    from clu.command import Command

    from gort.devices.core import GortDevice
    from gort.gort import Gort


__all__ = ["RemoteActor", "RemoteCommand", "ActorReply"]


class CommandSet(dict[str, "RemoteCommand"]):
    """A command set for a remote actor."""

    def __getattribute__(self, __name: str) -> RemoteCommand:
        if __name in self:
            return self[__name]
        return super().__getattribute__(__name)


class RemoteActor:
    """A programmatic representation of a remote actor."""

    def __init__(self, client: Gort, name: str, device: GortDevice | None = None):
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
        """Refreshes the command list."""

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
        reply_callback: Callable[[AMQPReply], None] | None | Literal[False] = None,
        timeout: float | None = None,
        n_retries: int = 0,
        delay: float = 1,
        **kwargs,
    ):
        """Executes the remote command with some given arguments, allowing retries.

        If a command fails with a timeout or the command cannot be parsed or does
        not exist, and error is raised immediately without retries.

        Parameters
        ----------
        args,kwargs
            Arguments to pass to :obj:`.get_command_string`.
        reply_callback
            Function to call with each reply received from the command.
        timeout
            Maximum time allowed for the command to complete.
        n_retries
            The maximum number of attempts before giving up.
        delay
            The delay between attempts, in seconds. This delay is increased for the
            second and successive retries using an exponential backoff.

        """

        retrier = Retrier(
            max_attempts=n_retries,
            delay=delay,
            on_retry=self._log_command_retry,
            raise_on_exception_class=[GortTimeoutError, InvalidRemoteCommand],
        )

        return await retrier(self.run)(
            *args,
            reply_callback=reply_callback,
            timeout=timeout,
            **kwargs,
        )

    async def run(
        self,
        *args,
        reply_callback: Callable[[AMQPReply], None] | None | Literal[False] = None,
        timeout: float | None = None,
        **kwargs,
    ):
        """Build the remote command and run it.

        This method does not allow retries. Use :meth:`.__call__` for that, which
        should be generally preferred.

        """

        parent_string = ""
        if self._parent is not None:
            # Call parent chain without arguments. This is not bullet-proof for all
            # cases, but probably good enough for now.
            parent_string = self._parent.get_command_string() + " "

        if reply_callback is None and self._remote_actor.device is not None:
            reply_callback = self._remote_actor.device.log_replies

        cmd_string = parent_string + self.get_command_string(*args, **kwargs)
        cmd = await self._remote_actor.send_raw_command(
            cmd_string,
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
            error = actor_reply.get("error", None)
            if error and "does not exist or cannot be parsed" in error:
                raise InvalidRemoteCommand(
                    f"Command '{actor} {cmd_string}' does not "
                    "exist or cannot be parsed.",
                    command=cmd,
                    remote_command=self,
                )

            raise RemoteCommandError(
                f"Actor {actor!r} failed executing command {command_name!r}.",
                command=cmd,
                remote_command=self,
                reply=actor_reply,
            )

        return actor_reply

    def _log_command_retry(self, error: BaseException):
        """Logs a command retry."""

        actor = self._remote_actor.name
        command_name = self._name

        if not isinstance(error, RemoteCommandError) or error.reply is None:
            # This should not happen, but just in case.
            self._remote_actor.client.log.warning(
                f"Actor {actor!r} failed executing command {command_name!r}. Retrying.",
            )
            return

        reply_error = error.reply.get("error")
        error_message = f" {reply_error!s}" if reply_error is not None else ""
        if not error_message.endswith("."):
            error_message += "."

        self._remote_actor.client.log.warning(
            f"Actor {actor!r} failed executing command "
            f"{command_name!r}.{error_message}. Retrying.",
        )


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

    def get(self, key: str, default: Any = ...):
        """Returns the first occurrence of a keyword in the reply list."""

        for reply in self.replies:
            if key in reply:
                return reply[key]

        if default is not ...:
            return default

        raise KeyError(f"Keyword {key!r} not found in replies.")
