#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-08
# @Filename: maskbits.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from enum import Enum, Flag


__all__ = ["ErrorCodes", "GuiderStatus", "Notification"]


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


class GuiderStatus(Flag):
    """Maskbits with the guider status."""

    IDLE = 1 << 0
    ACQUIRING = 1 << 1
    GUIDING = 1 << 2
    EXPOSING = 1 << 3
    PROCESSING = 1 << 4
    CORRECTING = 1 << 5
    STOPPING = 1 << 6
    FAILED = 1 << 7
    WAITING = 1 << 8
    DRIFTING = 1 << 9

    NON_IDLE = (
        ACQUIRING
        | GUIDING
        | EXPOSING
        | PROCESSING
        | CORRECTING
        | STOPPING
        | WAITING
        | DRIFTING
    )

    def get_names(self):
        """Returns a list of active bit names."""

        return [bit.name for bit in GuiderStatus if self & bit and bit.name]

    def __repr__(self):
        return str(" | ".join(self.get_names()))


class Notification(Enum):
    """Enumeration with the notification types."""

    UNCATEGORISED = 0
