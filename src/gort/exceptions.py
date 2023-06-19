#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: exceptions.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)


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


class GortWarning(Warning):
    """Base warning for Gort."""


class GortUserWarning(UserWarning, GortWarning):
    """The primary warning class."""

    pass
