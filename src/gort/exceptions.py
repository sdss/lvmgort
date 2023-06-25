#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: exceptions.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

import inspect


class GortError(Exception):
    """A custom core GortError exception"""

    pass


class GortTimeout(GortError):
    """Raised if a timeout occurs."""

    pass


class GortNotImplemented(GortError):
    """A custom exception for not yet implemented features."""

    def __init__(self, message=None):
        message = "This feature is not implemented yet." if not message else message

        super(GortNotImplemented, self).__init__(message)


class GortDeviceError(GortError):
    """A device error, which appends the name of the device to the error message."""

    def __init__(self, message: str | None = None) -> None:
        from gort.gort import GortDevice

        if message is not None:
            stack = inspect.stack()
            f_locals = stack[1][0].f_locals

            if "self" in f_locals:
                obj = f_locals["self"]
                name = getattr(obj, "name", None)
                if issubclass(obj.__class__, GortDevice) and name is not None:
                    message = f"({name}) {message}"

        super().__init__(message)


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


class GortWarning(Warning):
    """Base warning for Gort."""

    pass


class GortUserWarning(UserWarning, GortWarning):
    """The primary warning class."""

    pass
