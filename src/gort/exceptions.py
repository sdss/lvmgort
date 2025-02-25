#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: exceptions.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import inspect

from typing import TYPE_CHECKING, ClassVar

from gort.enums import ErrorCode


if TYPE_CHECKING:
    from clu import Command

    from gort.remote import ActorReply, RemoteCommand


def decapitalize_first_letter(s, upper_rest=False):
    return "".join([s[:1].lower(), (s[1:].upper() if upper_rest else s[1:])])


class GortError(Exception):
    """A custom core GortError exception."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = ErrorCode.UNCATEGORISED_ERROR
    EMIT_EVENT: ClassVar[bool] = True

    def __init__(
        self,
        message: str | None = None,
        error_code: int | ErrorCode | None = None,
        payload: dict = {},
    ):
        try:
            self.error_code = ErrorCode(error_code or self.DEFAULT_ERROR_CODE)
        except ValueError:
            self.error_code = ErrorCode.UNKNOWN_ERROR
            error_code = self.error_code.value

        self.payload = payload

        prefix = f"Error {self.error_code.value} ({self.error_code.name})"
        if message is not None and message != "":
            message = decapitalize_first_letter(message)
            super().__init__(f"{prefix}: {message}")
        else:
            super().__init__(prefix)


class OverwatcherError(GortError):
    """An error in the overwatcher."""

    pass


class TroubleshooterCriticalError(OverwatcherError):
    """A critical error in the troubleshooter that will shut down the system."""

    def __init__(self, *args, close_dome: bool = False, **kwargs):
        self.close_dome = close_dome
        super().__init__(*args, **kwargs)


class TroubleshooterTimeoutError(OverwatcherError):
    """The troubleshooter timed out while running a recipe."""

    pass


class RemoteCommandError(GortError):
    """An error in a remote command to an actor."""

    EMIT_EVENT = False

    def __init__(
        self,
        message: str | None,
        command: Command,
        remote_command: RemoteCommand,
        reply: ActorReply | None = None,
    ):
        self.command = command
        self.remote_command = remote_command
        self.actor = remote_command._remote_actor.name
        self.reply = reply

        super().__init__(message, error_code=ErrorCode.COMMAND_FAILED)


class InvalidRemoteCommand(GortError):
    """A command that does not exist or cannot be parsed."""

    def __init__(
        self,
        message: str | None,
        command: Command,
        remote_command: RemoteCommand,
    ):
        self.command = command
        self.remote_command = remote_command
        self.actor = remote_command._remote_actor.name

        super().__init__(message, error_code=ErrorCode.COMMAND_FAILED)


class GortTimeoutError(GortError):
    """A timeout error, potentially associated with a timed out remote command."""

    def __init__(
        self,
        message: str | None,
        command: Command | None = None,
        remote_command: RemoteCommand | None = None,
    ):
        self.command = command
        self.remote_command = remote_command
        self.actor = remote_command._remote_actor.name if remote_command else None

        if self.remote_command:
            error_code = ErrorCode.COMMAND_TIMEDOUT
        else:
            error_code = ErrorCode.TIMEOUT

        super().__init__(message, error_code=error_code)


class GortTimeout(GortError):
    """Raised if a timeout occurs."""

    pass


class GortNotImplemented(GortError):
    """A custom exception for not yet implemented features."""

    DEFAULT_ERROR_CODE = ErrorCode.NOT_IMPLEMENTED

    def __init__(self, message: str | None = None):
        message = "This feature is not implemented yet." if not message else message

        super(GortNotImplemented, self).__init__(message)


class GortDeviceError(GortError):
    """A device error, which appends the name of the device to the error message."""

    DEFAULT_ERROR_CODE = ErrorCode.DEVICE_ERROR

    def __init__(
        self,
        message: str | None = None,
        error_code: int | ErrorCode | None = None,
        payload: dict = {},
    ) -> None:
        from gort.gort import GortDevice

        if message is not None:
            stack = inspect.stack()
            f_locals = stack[1][0].f_locals

            if "self" in f_locals:
                obj = f_locals["self"]
                name = getattr(obj, "name", None)
                if issubclass(obj.__class__, GortDevice) and name is not None:
                    message = f"({name}) {message}"

        super().__init__(message, error_code=error_code, payload=payload)


class GortEnclosureError(GortDeviceError):
    """Enclosure-related error."""

    DEFAULT_ERROR_CODE = ErrorCode.ENCLOSURE_ERROR


class GortNPSError(GortDeviceError):
    """NPS-related error."""

    DEFAULT_ERROR_CODE = ErrorCode.NPS_ERROR


class GortGuiderError(GortDeviceError):
    """Guider-related error."""

    DEFAULT_ERROR_CODE = ErrorCode.GUIDER_ERROR


class GortSpecError(GortDeviceError):
    """Spectrograph-related error."""

    DEFAULT_ERROR_CODE = ErrorCode.SPECTROGRAPH_ERROR


class GortAGError(GortDeviceError):
    """AG-related error."""

    DEFAULT_ERROR_CODE = ErrorCode.AG_ERROR


class GortTelescopeError(GortDeviceError):
    """Telescope-related error."""

    DEFAULT_ERROR_CODE = ErrorCode.TELESCOPE_ERROR


class TileError(GortError):
    """An error associated with a `.Tile`."""

    DEFAULT_ERROR_CODE = ErrorCode.SCHEDULER_TILE_ERROR


class GortObserverError(GortError):
    """An error associated with the `.Observer`."""

    DEFAULT_ERROR_CODE = ErrorCode.OBSERVER_ERROR


class GortObserverCancelledError(GortObserverError):
    """An error issued when the observer is cancelled."""

    EMIT_EVENT = False  # Only used to trigger a cancellation of the observing loop.

    pass


class GortWarning(Warning):
    """Base warning for Gort."""

    pass


class GortUserWarning(UserWarning, GortWarning):
    """The primary warning class."""

    pass
