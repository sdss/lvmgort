# !usr/bin/env python
# -*- coding: utf-8 -*-
#
# Licensed under a 3-clause BSD license.
#
# @Author: Brian Cherinka
# @Date:   2017-12-05 12:01:21
# @Last modified by:   Brian Cherinka
# @Last Modified time: 2017-12-05 12:19:32

from __future__ import print_function, division, absolute_import


class LvmbrainError(Exception):
    """A custom core Lvmbrain exception"""

    def __init__(self, message=None):

        message = 'There has been an error' \
            if not message else message

        super(LvmbrainError, self).__init__(message)


class LvmbrainNotImplemented(LvmbrainError):
    """A custom exception for not yet implemented features."""

    def __init__(self, message=None):

        message = 'This feature is not implemented yet.' \
            if not message else message

        super(LvmbrainNotImplemented, self).__init__(message)


class LvmbrainAPIError(LvmbrainError):
    """A custom exception for API errors"""

    def __init__(self, message=None):
        if not message:
            message = 'Error with Http Response from Lvmbrain API'
        else:
            message = 'Http response error from Lvmbrain API. {0}'.format(message)

        super(LvmbrainAPIError, self).__init__(message)


class LvmbrainApiAuthError(LvmbrainAPIError):
    """A custom exception for API authentication errors"""
    pass


class LvmbrainMissingDependency(LvmbrainError):
    """A custom exception for missing dependencies."""
    pass


class LvmbrainWarning(Warning):
    """Base warning for Lvmbrain."""


class LvmbrainUserWarning(UserWarning, LvmbrainWarning):
    """The primary warning class."""
    pass


class LvmbrainSkippedTestWarning(LvmbrainUserWarning):
    """A warning for when a test is skipped."""
    pass


class LvmbrainDeprecationWarning(LvmbrainUserWarning):
    """A warning for deprecated features."""
    pass
