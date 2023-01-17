#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: exceptions.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)


class LVMBrainErrorError(Exception):
    """A custom core LVMBrainError exception"""

    def __init__(self, message=None):
        message = "There has been an error" if not message else message

        super(LVMBrainErrorError, self).__init__(message)


class LVMBrainErrorNotImplemented(LVMBrainErrorError):
    """A custom exception for not yet implemented features."""

    def __init__(self, message=None):
        message = "This feature is not implemented yet." if not message else message

        super(LVMBrainErrorNotImplemented, self).__init__(message)


class LVMBrainWarning(Warning):
    """Base warning for lvmbrain."""


class LVMBrainUserWarning(UserWarning, LVMBrainWarning):
    """The primary warning class."""

    pass
