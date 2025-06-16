#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-08-05
# @Filename: commands.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import math
import time

from typing import TYPE_CHECKING, Any

import click

from clu.parsers.click import command_parser as overwatcher_cli

from gort.overwatcher.calibrations import CalibrationState


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
                "idle": overwatcher.state.idle,
                "observing": overwatcher.state.observing,
                "calibrating": overwatcher.state.calibrating,
                "safe": overwatcher.state.safe,
                "alerts": overwatcher.state.alerts,
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
@click.option(
    "--now",
    is_flag=True,
    help="Stops observing immediately.",
)
@click.option(
    "--close-dome",
    is_flag=True,
    help="Closes the dome after stopping observing.",
)
async def disable(
    command: OverwatcherCommand,
    now: bool = False,
    close_dome: bool = False,
):
    """Disables the overwatcher."""

    overwatcher = command.actor.overwatcher

    if close_dome and not now:
        return command.fail("--now is required when using --close.")

    if now:
        await overwatcher.shutdown(
            "user disabled the Overwatcher.",
            close_dome=close_dome,
            disable_overwatcher=True,
        )
        return command.finish(text="Overwatcher has been disabled.")

    if overwatcher.state.enabled:
        # This will cancel the observing loop after the current tile.
        overwatcher.state.enabled = False
        await overwatcher.notify("Overwatcher has been disabled.")

    return command.finish(text="Overwatcher has been disabled.")


@overwatcher_cli.command()
async def reset(command: OverwatcherCommand):
    """Resets the Overwatcher."""

    overwatcher = command.actor.overwatcher

    overwatcher.dome.reset()
    for actor in overwatcher.gort.actors.values():
        await actor.refresh()

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


@calibrations.command(name="list")
async def list_(command: OverwatcherCommand):
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
                "disabled": cal.model.disabled,
            }
        )

    return command.finish(calibrations_sjd=schedule.sjd, calibrations=response)


@calibrations.command(name="reset")
@click.argument("CALIBRATION", type=str, required=False)
@click.option("--include-done", is_flag=True, help="Include done calibrations.")
async def calibrations_reset(
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


@calibrations.command()
@click.option(
    "--now",
    is_flag=True,
    help="Starts the next calibration immediately.",
)
@click.option(
    "--remove",
    is_flag=True,
    help="Removes the scheduled long-term calibrations.",
)
async def schedule_long_term_calibrations(
    command: OverwatcherCommand,
    now: bool = False,
    remove: bool = False,
):
    """Schedules long-term calibrations."""

    overwatcher = command.actor.overwatcher
    calibrations = overwatcher.calibrations

    long_term_cals = calibrations.schedule.get_calibration("long_term_calibrations")
    if long_term_cals is None:
        return command.fail(error="Long-term calibrations are not defined.")

    if remove:
        long_term_cals.model.disabled = True
        return command.finish(text="Long-term calibrations have been unscheduled.")

    # The long term calibrations are disabled by default. Enable them and make sure
    # that they are taken even if they have failed before or been done already.
    long_term_cals.model.disabled = False
    await long_term_cals.record_state(CalibrationState.WAITING)

    if now:
        command.info("Stopping the current observing loop to allow calibrations.")
        await overwatcher.observer.stop_observing(
            immediate=True,
            reason="Scheduling long-term calibrations",
        )

    return command.finish(text="Long-term calibrations have been scheduled.")


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
            "focusing": overwatcher.state.focusing,
            "troubleshooting": overwatcher.state.troubleshooting,
            "tile_id": tile_id,
            "dither_position": dither_position,
            "stage": stage,
            "standard_no": standard_no,
        }
    )


@observer.command()
async def schedule_focus_sweep(command: OverwatcherCommand):
    """Schedules a focus sweep before the next tile."""

    command.actor.overwatcher.observer.force_focus = True

    return command.finish()


@overwatcher_cli.command()
@click.option(
    "--force",
    is_flag=True,
    help="Forces the overwatcher to run the complete "
    "shutdown sequence even if the dome is already closed.",
)
async def shutdown(command: OverwatcherCommand, force: bool = False):
    """Disables the overwatcher, stops observing, and closes the dome."""

    await command.actor.overwatcher.shutdown(force=force)
    return command.finish()


@overwatcher_cli.group()
def transparency():
    """Transparency commands."""

    pass


@transparency.command(name="status")
async def transparency_status(command: OverwatcherCommand):
    """Reports the transparency status of the science telescope."""

    overwatcher = command.actor.overwatcher
    transparency = overwatcher.transparency

    now = time.time()
    if transparency.last_updated < now - 120:
        command.warning("Transparency data is stale.")
        return command.finish(
            transparency={
                "telescope": "sci",
                "mean_zp": None,
                "quality": "unknown",
                "trend": "unknown",
            }
        )

    zp = transparency.zero_point["sci"]

    return command.finish(
        transparency={
            "telescope": "sci",
            "mean_zp": None if math.isnan(zp) else round(zp, 2),
            "quality": transparency.get_quality_string("sci"),
            "trend": transparency.get_trend_string("sci"),
        }
    )


@transparency.command()
async def start_monitoring(command: OverwatcherCommand):
    """Starts monitoring the transparency."""

    overwatcher = command.actor.overwatcher

    if not overwatcher.transparency.is_monitoring():
        await overwatcher.transparency.start_monitoring()
        command.info("Starting transparency monitoring.")

    elapsed: float = 0
    while True:
        if not overwatcher.transparency.is_monitoring():
            return command.finish("Transparency monitoring has been stopped.")

        await asyncio.sleep(1)
        elapsed += 1

        if elapsed >= 30:
            await command.child_command("transparency status")
            elapsed = 0


@transparency.command()
async def stop_monitoring(command: OverwatcherCommand):
    """Stops monitoring the transparency."""

    overwatcher = command.actor.overwatcher

    if overwatcher.transparency.is_monitoring():
        await overwatcher.transparency.stop_monitoring()

    return command.finish()


@overwatcher_cli.command()
@click.option(
    "-s",
    "--show",
    is_flag=True,
    help="Shows the configuration after reloading it.",
)
async def reload_config(command: OverwatcherCommand, show: bool = False):
    """Reloads the Overwatcher configuration."""

    overwatcher = command.actor.overwatcher

    overwatcher.config.reload()

    if not show:
        return command.finish(text="Configuration reloaded.")

    return command.finish(configuration=dict(overwatcher.config))
