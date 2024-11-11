#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-08
# @Filename: maskbits.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from enum import Enum, Flag, ReprEnum, auto


__all__ = ["ErrorCode", "GuiderStatus", "Event", "ObserverStageStatus"]


class ErrorCode(Enum):
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
    CALIBRATION_ERROR = 900
    UNKNOWN_ERROR = 9999

    def is_telescope_error(self):
        """Returns True if the error is related to the telescope."""

        return self.value >= 100 and self.value < 200

    def is_ag_error(self):
        """Returns True if the error is related to the autoguider."""

        return self.value >= 200 and self.value < 300

    def is_spectrograph_error(self):
        """Returns True if the error is related to the spectrograph."""

        return self.value >= 300 and self.value < 400

    def is_nps_error(self):
        """Returns True if the error is related to the NPS."""

        return self.value >= 400 and self.value < 500

    def is_enclosure_error(self):
        """Returns True if the error is related to the enclosure."""

        return self.value >= 500 and self.value < 600

    def is_guiding_error(self):
        """Returns True if the error is related to the guider."""

        return self.value >= 600 and self.value < 700

    def is_scheduler_error(self):
        """Returns True if the error is related to the scheduler."""

        return self.value >= 700 and self.value < 800

    def is_observer_error(self):
        """Returns True if the error is related to the observer."""

        return self.value >= 800 and self.value < 900


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


class UpperStrEnum(str, ReprEnum):
    """A string enum in which the auto value is the uppercase name."""

    @staticmethod
    def _generate_next_value_(name, *_):
        return name.upper()


class Event(UpperStrEnum):
    """Enumeration with the event types."""

    ERROR = auto()
    RECIPE_START = auto()
    RECIPE_END = auto()
    RECIPE_FAILED = auto()
    OBSERVER_NEW_TILE = auto()
    OBSERVER_STAGE_RUNNING = auto()
    OBSERVER_STAGE_DONE = auto()
    OBSERVER_STAGE_FAILED = auto()
    OBSERVER_ACQUISITION_START = auto()
    OBSERVER_ACQUISITION_DONE = auto()
    OBSERVER_STANDARD_ACQUISITION_FAILED = auto()
    DOME_OPENING = auto()
    DOME_OPEN = auto()
    DOME_CLOSING = auto()
    DOME_CLOSED = auto()
    EMERGENCY_SHUTDOWN = auto()
    UNCATEGORISED = auto()


class ObserverStageStatus(UpperStrEnum):
    """An enumeration of observer stages."""

    WAITING = auto()
    RUNNING = auto()
    DONE = auto()
    FAILED = auto()
    CANCELLED = auto()
