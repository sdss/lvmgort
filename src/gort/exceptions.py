#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: exceptions.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import inspect
from enum import Enum

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from clu import Command

    from gort.core import RemoteCommand


def decapitalize_first_letter(s, upper_rest=False):
    return "".join([s[:1].lower(), (s[1:].upper() if upper_rest else s[1:])])


class ErrorCodes(Enum):
    """List of error codes."""

    UNCATEGORISED_ERROR = 0
    NOT_IMPLEMENTED = 1
    COMMAND_FAILED = 2
    COMMAND_TIMEDOUT = 3
    USAGE_ERROR = 4
    TIMEOUT = 5
    CANNOT_MOVE_LOCAL_MODE = 101
    FAILED_REACHING_COMMANDED_POSITION = 102
    INVALID_TELESCOPE_POSITION = 103
    FIBSEL_INVALID_POSITION = 201
    SECTROGRAPH_FAILED_EXPOSING = 301
    SECTROGRAPH_NOT_IDLE = 302
    INVALID_CALIBRATION_SEQUENCE = 303
    LOCAL_MODE_FAILED = 501
    DOOR_STATUS_FAILED = 502
    INVALID_PIXEL_NAME = 610
    ACQUISITION_FAILED = 801
    UNKNOWN_ERROR = 999


class GortError(Exception):
    """A custom core GortError exception"""

    def __init__(self, message: str | None = None, error_code: int | ErrorCodes = 0):
        try:
            self.error_code = ErrorCodes(error_code)
        except ValueError:
            self.error_code = ErrorCodes.UNKNOWN_ERROR
            error_code = self.error_code.value

        prefix = f"Error {self.error_code.value} ({self.error_code.name})"
        if message is not None and message != "":
            message = decapitalize_first_letter(message)
            super().__init__(f"{prefix}: {message}")
        else:
            super().__init__(prefix)


class RemoteCommandError(GortError):
    """An error in a remote command to an actor."""

    def __init__(
        self,
        message: str | None,
        command: Command,
        remote_command: RemoteCommand,
    ):
        self.command = command
        self.remote_command = remote_command
        self.actor = remote_command._remote_actor.name

        super().__init__(message, error_code=ErrorCodes.COMMAND_FAILED)


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
            error_code = ErrorCodes.COMMAND_TIMEDOUT
        else:
            error_code = ErrorCodes.TIMEOUT

        super().__init__(message, error_code=error_code)


class GortTimeout(GortError):
    """Raised if a timeout occurs."""

    pass


class GortNotImplemented(GortError):
    """A custom exception for not yet implemented features."""

    def __init__(self, message: str | None = None):
        message = "This feature is not implemented yet." if not message else message

        super(GortNotImplemented, self).__init__(message, error_code=1)


class GortDeviceError(GortError):
    """A device error, which appends the name of the device to the error message."""

    def __init__(
        self,
        message: str | None = None,
        error_code: int | ErrorCodes = 0,
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

        super().__init__(message, error_code=error_code)


class GortEnclosureError(GortDeviceError):
    """Enclosure-related error."""

    pass


class GortNPSError(GortDeviceError):
    """NPS-related error."""

    pass


class GortGuiderError(GortDeviceError):
    """Guider-related error."""

    pass


class GortSpecError(GortDeviceError):
    """Spectrograph-related error."""

    pass


class GortAGError(GortDeviceError):
    """AG-related error."""

    pass


class GortTelescopeError(GortDeviceError):
    """Telescope-related error."""

    pass


class TileError(GortError):
    """An error associated with a `.Tile`."""

    pass


class GortObserverError(GortError):
    """An error associated with the `.Observer`."""

    pass


class GortWarning(Warning):
    """Base warning for Gort."""

    pass


class GortUserWarning(UserWarning, GortWarning):
    """The primary warning class."""

    pass
