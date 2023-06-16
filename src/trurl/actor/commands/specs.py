#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-04-05
# @Filename: specs.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import click

from trurl.actor import TrurlCommand

from . import trurl_parser


__all__ = ["spec"]


@trurl_parser.group()
def spec():
    """Handles multiple spectrographs."""


@spec.command()
@click.argument("EXPOSURE-TIME", type=float, required=False)
@click.option("--flavour", type=str, help="The exposure type.")
@click.option(
    "--spec",
    type=click.Choice(["sp1", "sp2", "sp3"]),
    help="Spectrograph to command. Otherwise exposes all connected spectrographs.",
)
async def expose(
    command: TrurlCommand,
    exposure_time: float | None = None,
    flavour: str | None = None,
    spec: str | None = None,
):
    """Exposes the spectrographs."""

    sp = command.actor.trurl.specs
    if spec is not None:
        sp = command.actor.trurl.specs[spec]

    await sp.expose(exposure_time=exposure_time, flavour=flavour)

    return command.finish()
