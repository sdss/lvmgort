#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-08-05
# @Filename: commands.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING

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
                "enabled": overwatcher.state.enabled,
                "observing": overwatcher.state.observing,
                "calibrating": overwatcher.state.calibrating,
                "allow_dome_calibrations": overwatcher.state.allow_dome_calibrations,
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
