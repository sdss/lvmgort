#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: exceptions.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import inspect
from enum import Enum

from typing import TYPE_CHECKING, ClassVar

from gort.maskbits import Event
from gort.pubsub import notify_event


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
    OVERATCHER_RUNNING = 6
    DEVICE_ERROR = 10
    TELESCOPE_ERROR = 100
    CANNOT_MOVE_LOCAL_MODE = 101
    FAILED_REACHING_COMMANDED_POSITION = 102
    INVALID_TELESCOPE_POSITION = 103
    FIBSEL_INVALID_POSITION = 110
    AG_ERROR = 200
    SPECTROGRAPH_ERROR = 300
    SECTROGRAPH_FAILED_EXPOSING = 301
    SECTROGRAPH_NOT_IDLE = 302
    INVALID_CALIBRATION_SEQUENCE = 303
    NPS_ERROR = 400
    ENCLOSURE_ERROR = 500
    LOCAL_MODE_FAILED = 501
    DOOR_STATUS_FAILED = 502
    GUIDER_ERROR = 600
    INVALID_PIXEL_NAME = 601
    SCHEDULER_UNCATEGORISED = 701
    SCHEDULER_TILE_ERROR = 702
    SCHEDULER_CANNOT_FIND_TILE = 703
    OBSERVER_ERROR = 800
    ACQUISITION_FAILED = 801
    UNKNOWN_ERROR = 9999


class GortError(Exception):
    """A custom core GortError exception."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCodes] = ErrorCodes.UNCATEGORISED_ERROR

    def __init__(
        self,
        message: str | None = None,
        error_code: int | ErrorCodes | None = None,
        payload: dict = {},
        emit_event: bool = True,
    ):
        try:
            self.error_code = ErrorCodes(error_code or self.DEFAULT_ERROR_CODE)
        except ValueError:
            self.error_code = ErrorCodes.UNKNOWN_ERROR
            error_code = self.error_code.value

        self.payload = payload

        if emit_event:
            event_payload = self.payload.copy()
            event_payload["error"] = message
            event_payload["error_code"] = self.error_code.value

            asyncio.create_task(notify_event(Event.ERROR, payload=event_payload))

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

    DEFAULT_ERROR_CODE = ErrorCodes.NOT_IMPLEMENTED

    def __init__(self, message: str | None = None):
        message = "This feature is not implemented yet." if not message else message

        super(GortNotImplemented, self).__init__(message)


class GortDeviceError(GortError):
    """A device error, which appends the name of the device to the error message."""

    DEFAULT_ERROR_CODE = ErrorCodes.DEVICE_ERROR

    def __init__(
        self,
        message: str | None = None,
        error_code: int | ErrorCodes | None = None,
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

    DEFAULT_ERROR_CODE = ErrorCodes.ENCLOSURE_ERROR


class GortNPSError(GortDeviceError):
    """NPS-related error."""

    DEFAULT_ERROR_CODE = ErrorCodes.NPS_ERROR


class GortGuiderError(GortDeviceError):
    """Guider-related error."""

    DEFAULT_ERROR_CODE = ErrorCodes.GUIDER_ERROR


class GortSpecError(GortDeviceError):
    """Spectrograph-related error."""

    DEFAULT_ERROR_CODE = ErrorCodes.SPECTROGRAPH_ERROR


class GortAGError(GortDeviceError):
    """AG-related error."""

    DEFAULT_ERROR_CODE = ErrorCodes.AG_ERROR


class GortTelescopeError(GortDeviceError):
    """Telescope-related error."""

    DEFAULT_ERROR_CODE = ErrorCodes.TELESCOPE_ERROR


class TileError(GortError):
    """An error associated with a `.Tile`."""

    DEFAULT_ERROR_CODE = ErrorCodes.SCHEDULER_TILE_ERROR


class GortObserverError(GortError):
    """An error associated with the `.Observer`."""

    DEFAULT_ERROR_CODE = ErrorCodes.OBSERVER_ERROR


class GortWarning(Warning):
    """Base warning for Gort."""

    pass


class GortUserWarning(UserWarning, GortWarning):
    """The primary warning class."""

    pass
