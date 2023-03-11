#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: exceptions.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)


class TrurlError(Exception):
    """A custom core TrurlError exception"""

    pass


class TrurlTimeout(TrurlError):
    """Raised if a timeout occurs."""

    pass


class TrurlNotImplemented(TrurlError):
    """A custom exception for not yet implemented features."""

    def __init__(self, message=None):
        message = "This feature is not implemented yet." if not message else message

        super(TrurlNotImplemented, self).__init__(message)


class TrurlWarning(Warning):
    """Base warning for Trurl."""


class TrurlUserWarning(UserWarning, TrurlWarning):
    """The primary warning class."""

    pass
