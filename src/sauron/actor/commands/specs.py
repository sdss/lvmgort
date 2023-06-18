#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-04-05
# @Filename: specs.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

import click

from sauron.actor import TrurlCommand

from . import trurl_parser


__all__ = ["spec"]


@trurl_parser.group()
def spec():
    """Handles multiple spectrographs."""


@spec.command(context_settings=dict(ignore_unknown_options=True))
@click.option(
    "--spec",
    type=click.Choice(["sp1", "sp2", "sp3"]),
    help="Spectrograph to command. Otherwise exposes all connected spectrographs.",
)
@click.argument("extra_opts", type=click.UNPROCESSED, nargs=-1)
async def expose(
    command: TrurlCommand,
    spec: str | None = None,
    extra_opts: list = [],
):
    """Exposes the spectrographs. Accepts the same flags as lvmscp expose."""

    # Here we don't use the unclick interface since extra_opts is a string that
    # we can pass directly to each one of the lvmscp actors. But we still need to
    # specify the seqno.

    specs = command.actor.trurl.specs

    if "-s" in extra_opts or "--seqno" in extra_opts:
        return command.fail("--seqno/-s cannot be manually specified in trurl.")

    seqno = specs.get_seqno()
    command_str = f"expose -s {seqno} " + " ".join(extra_opts)

    if spec is None:
        names = specs.names
        scp_commands = [specs[name].scp.send_raw_command(command_str) for name in names]
    else:
        scp_commands = [specs[spec].scp.send_raw_command(command_str)]

    command.info(f"Exposing with seqno={seqno}")

    await asyncio.gather(*scp_commands)

    return command.finish()
