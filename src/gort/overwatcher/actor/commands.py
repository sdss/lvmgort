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

import click

from clu.parsers.click import command_parser as overwatcher_cli

from gort.overwatcher.calibration import CalibrationState


if TYPE_CHECKING:
    from gort.overwatcher.actor.actor import OverwatcherCommand


@overwatcher_cli.command()
async def status(command: OverwatcherCommand):
    """Reports the status of the overwatcher."""

    overwatcher = command.actor.overwatcher

    return command.finish(
        message={
            "status": {
                "sjd": overwatcher.ephemeris.sjd,
                "running": True,
                "enabled": overwatcher.state.enabled,
                "observing": overwatcher.state.observing,
                "calibrating": overwatcher.state.calibrating,
                "safe": overwatcher.state.safe,
                "night": overwatcher.state.night,
                "allow_calibrations": overwatcher.state.allow_calibrations,
                "dry_run": overwatcher.state.dry_run,
            }
        }
    )


@overwatcher_cli.command()
async def enable(command: OverwatcherCommand):
    """Enables the overwatcher."""

    overwatcher = command.actor.overwatcher

    if not overwatcher.state.enabled:
        overwatcher.state.enabled = True
        await overwatcher.notify("Overwatcher has been enabled.")

    return command.finish()


@overwatcher_cli.command()
@click.option("--now", is_flag=True, help="Stops observing immediately.")
async def disable(command: OverwatcherCommand, now: bool = False):
    """Disables the overwatcher."""

    overwatcher = command.actor.overwatcher

    if now:
        await overwatcher.observer.stop_observing(
            immediate=True,
            reason="user disabled observing mode",
        )

    if overwatcher.state.enabled:
        overwatcher.state.enabled = False
        await overwatcher.notify("Overwatcher has been disabled.")

    return command.finish()


@overwatcher_cli.group()
def calibrations(*_):
    """Handles the automated calibrations."""

    pass


@calibrations.command()
async def enable_calibrations(command: OverwatcherCommand):
    """Allows dome calibrations."""

    overwatcher = command.actor.overwatcher

    if not overwatcher.state.allow_calibrations:
        overwatcher.state.allow_calibrations = True
        await overwatcher.notify("Calibrations have been enabled.")

    return command.finish()


@calibrations.command()
async def disable_calibrations(command: OverwatcherCommand):
    """Disallows calibrations."""

    overwatcher = command.actor.overwatcher

    if overwatcher.state.allow_calibrations:
        overwatcher.state.allow_calibrations = False
        await overwatcher.notify("Calibrations have been disabled.")

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

    return command.finish(calibrations_sjd=schedule.sjd, calibrations=response)


@calibrations.command()
@click.argument("CALIBRATION", type=str, required=False)
@click.option("--include-done", is_flag=True, help="Include done calibrations.")
async def reset(
    command: OverwatcherCommand,
    calibration: str | None = None,
    include_done: bool = False,
):
    """Resets the status of a calibration."""

    overwatcher = command.actor.overwatcher

    schedule = overwatcher.calibrations.schedule
    for cal in schedule.calibrations:
        if calibration is not None and cal.name != calibration:
            continue

        if (cal.state == CalibrationState.DONE) and not include_done:
            continue

        cal.state = CalibrationState.WAITING

    overwatcher.calibrations.schedule.update_schedule(reset=True)

    return command.finish()


@overwatcher_cli.group()
def observer(*_):
    """Commands the overwatcher observer."""

    pass


@observer.command(name="status")
async def observer_status(command: OverwatcherCommand):
    """Reports the status of the observer."""

    overwatcher = command.actor.overwatcher
    overwatcher_observer = overwatcher.observer
    gort_observer = overwatcher.gort.observer

    tile = gort_observer._tile

    tile_id: int | None = None
    dither_position: int | None = None
    stage: str | None = None
    standard_no: int | None = None

    if overwatcher_observer.is_observing and gort_observer.is_running() and tile:
        tile_id = tile.tile_id
        dither_position = overwatcher.gort.observer.dither_position

        if standards := overwatcher.gort.observer.standards:
            standard_no = standards.current_standard

        stage = gort_observer.get_running_stage()

    return command.finish(
        observer_status={
            "observing": overwatcher_observer.is_observing,
            "cancelling": overwatcher_observer.is_cancelling,
            "tile_id": tile_id,
            "dither_position": dither_position,
            "stage": stage,
            "standard_no": standard_no,
        }
    )
