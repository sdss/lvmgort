#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: __main__.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import sys

import click

from lvmbrain import __version__


@click.group(
    invoke_without_command=True,
)
@click.option(
    "--version",
    is_flag=True,
    help="Print version and exit.",
)
@click.pass_context
def lvmbrain(ctx: click.Context, version: bool = False):
    """HAL actor."""

    if version is True:
        click.echo(__version__)
        sys.exit(0)


if __name__ == "__main__":
    lvmbrain()
