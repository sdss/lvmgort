#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-13
# @Filename: telescopes.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import click

from trurl.actor import TrurlCommand

from . import trurl_parser


__all__ = ["telescope"]


@trurl_parser.group()
def telescope():
    """Handles multiple telescopes."""


@telescope.command()
@click.option("--disable", is_flag=True, help="Disable telescopes after parking.")
async def park(command: TrurlCommand, disable: bool = False):
    """Park the telescopes."""

    await command.actor.trurl.telescopes.park(disable=disable)

    return command.finish()


@telescope.command()
@click.argument("RA_H", type=float, required=False)
@click.argument("DEC_D", type=float, required=False)
@click.argument("--named-position", "-n", type=str, help="Go to named position.")
@click.option("--altaz", is_flag=True, help="Coordinates are Alt/Az in de degrees.")
@click.option(
    "--telescope",
    type=click.Choice(["sci", "skyw", "skye", "spec"]),
    help="Telescope to command. Otherwise sends all the telescopes to "
    "the same location.",
)
@click.option("--kmirror/--no-kmirror", default=True, help="Start k-mirror tracking.")
async def goto(
    command: TrurlCommand,
    ra_h: float | None = None,
    dec_d: float | None = None,
    named_position: str | None = None,
    altaz: bool = False,
    telescope: str | None = None,
    kmirror: bool = True,
):
    """Sends the telescopes to a given location on the sky."""

    if named_position is None and (ra_h is None or dec_d is None):
        raise click.UsageError(
            "RA_H DEC_H are required unless --named-position is used."
        )

    if named_position is None:
        tel = command.actor.trurl.telescopes
        if telescope is not None:
            tel = command.actor.trurl.telescopes[telescope]

        if altaz:
            await tel.goto_coordinates(alt=ra_h, az=dec_d, kmirror=kmirror)
        else:
            await tel.goto_coordinates(ra=ra_h, dec=dec_d, kmirror=kmirror)

    else:
        if telescope is not None:
            raise click.UsageError("--telescope and --named-position are incompatible.")

        tels = command.actor.trurl.telescopes
        await tels.goto_named_position(named_position)

    return command.finish()
