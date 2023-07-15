#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: exceptions.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

import inspect
from enum import Enum


def decapitalize_first_letter(s, upper_rest=False):
    return "".join([s[:1].lower(), (s[1:].upper() if upper_rest else s[1:])])


class ErrorCodes(Enum):
    """List of error codes."""

    UNCATEGORISED_ERROR = 0
    NOT_IMPLEMENTED = 1
    COMMAND_FAILED = 2
    USAGE_ERROR = 3
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

        if message is not None and message != "":
            message = decapitalize_first_letter(message)
            super().__init__(f"Error {error_code} ({self.error_code.name}): {message}")
        else:
            super().__init__(f"Error {error_code} ({self.error_code.name})")


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
