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
@click.argument("RA_H", type=float)
@click.argument("DEC_D", type=float)
@click.option("--altaz", is_flag=True, help="Coordinates are Alt/Az in de degrees.")
@click.option(
    "--telescope",
    type=click.Choice(["sci", "skyw", "skye", "spec"]),
    help="Telescope to command. Otherwise sends all the telescopes to "
    "the same location.",
)
async def goto(
    command: TrurlCommand,
    ra_h: float,
    dec_d: float,
    altaz: bool = False,
    telescope: str | None = None,
):
    """Sends the telescopes to a given location on the sky."""

    tel = command.actor.trurl.telescopes
    if telescope is not None:
        tel = command.actor.trurl.telescopes[telescope]

    if altaz:
        await tel.goto(alt=ra_h, az=dec_d)
    else:
        await tel.goto(ra=ra_h, dec=dec_d)

    return command.finish()
