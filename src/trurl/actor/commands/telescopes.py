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


__all__ = ["telescopes"]


@trurl_parser.group()
def telescopes():
    """Handles multiple telescopes."""


@telescopes.command()
@click.option("--disable", is_flag=True, help="Disable telescopes after parking.")
async def park(command: TrurlCommand, disable: bool = False):
    """Park the telescopes."""

    await command.actor.trurl.telescopes.park(disable=disable)

    return command.finish()
