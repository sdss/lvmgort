#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: exceptions.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)


class LVMBrainError(Exception):
    """A custom core LVMBrainError exception"""

    pass


class LVMBrainTimeout(LVMBrainError):
    """Raised if a timeout occurs."""

    pass


class LVMBrainNotImplemented(LVMBrainError):
    """A custom exception for not yet implemented features."""

    def __init__(self, message=None):
        message = "This feature is not implemented yet." if not message else message

        super(LVMBrainNotImplemented, self).__init__(message)


class LVMBrainWarning(Warning):
    """Base warning for lvmbrain."""


class LVMBrainUserWarning(UserWarning, LVMBrainWarning):
    """The primary warning class."""

    pass
