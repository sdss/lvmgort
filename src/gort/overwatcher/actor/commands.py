#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-08-05
# @Filename: commands.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import time

from typing import TYPE_CHECKING, Any

from clu.parsers.click import command_parser as overwatcher_cli


if TYPE_CHECKING:
    from gort.overwatcher.actor.actor import OverwatcherCommand


@overwatcher_cli.command()
async def status(command: OverwatcherCommand):
    """Reports the status of the overwatcher."""

    overwatcher = command.actor.overwatcher

    return command.finish(
        message={
            "status": {
                "running": True,
                "enabled": overwatcher.state.enabled,
                "observing": overwatcher.state.observing,
                "calibrating": overwatcher.state.calibrating,
                "safe": overwatcher.state.safe,
                "night": overwatcher.state.night,
                "allow_dome_calibrations": overwatcher.state.allow_dome_calibrations,
                "dry_run": overwatcher.state.dry_run,
            }
        }
    )


@overwatcher_cli.command()
async def enable(command: OverwatcherCommand):
    """Enables the overwatcher."""

    overwatcher = command.actor.overwatcher
    overwatcher.state.enabled = True

    return command.finish()


@overwatcher_cli.command()
async def disable(command: OverwatcherCommand):
    """Disables the overwatcher."""

    overwatcher = command.actor.overwatcher
    overwatcher.state.enabled = False

    return command.finish()


@overwatcher_cli.group()
def calibrations(*_):
    """Handles the automated calibrations."""

    pass


@calibrations.command()
async def enable_dome_calibrations(command: OverwatcherCommand):
    """Allows dome calibrations."""

    overwatcher = command.actor.overwatcher
    overwatcher.state.allow_dome_calibrations = True

    return command.finish()


@calibrations.command()
async def disable_dome_calibrations(command: OverwatcherCommand):
    """Disallows dome calibrations."""

    overwatcher = command.actor.overwatcher
    overwatcher.state.allow_dome_calibrations = False

    return command.finish()


@calibrations.command()
async def list(command: OverwatcherCommand):
    """Lists the calibrations."""

    def format_timestamp(timestamp: float | None) -> str | None:
        if timestamp is None:
            return None

        return time.strftime("%H:%M:%S", time.gmtime(timestamp))

    cals_overwatcher = command.actor.overwatcher.calibrations
    schedule = cals_overwatcher.schedule
    calibrations = schedule.calibrations

    now = time.time()

    response: list[dict[str, Any]] = []
    for cal in calibrations:
        time_to_cal: float | None = None
        if not cal.is_finished() and cal.start_time is not None:
            time_to_cal = round(cal.start_time - now, 1)

        response.append(
            {
                "name": cal.name,
                "start_time": format_timestamp(cal.start_time),
                "max_start_time": format_timestamp(cal.max_start_time),
                "after": cal.model.after,
                "time_to_cal": time_to_cal,
                "status": cal.state.name.lower(),
                "requires_dome": cal.model.dome,
                "close_dome_after": cal.model.close_dome_after,
            }
        )

    return command.finish(calibrations=response)


@calibrations.command()
async def reset(command: OverwatcherCommand):
    """Resets the calibration schedule. Clears done calibrations."""

    overwatcher = command.actor.overwatcher
    overwatcher.calibrations.schedule.update_schedule(clear=True)

    return command.finish()
