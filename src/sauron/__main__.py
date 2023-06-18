#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-01-17
# @Filename: __main__.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import os
import sys

import click
from click_default_group import DefaultGroup

from sdsstools.daemonizer import DaemonGroup, cli_coro

from sauron import __version__
from sauron.actor.actor import TrurlActor


@click.group(
    cls=DefaultGroup,
    default="actor",
    default_if_no_args=True,
    invoke_without_command=True,
)
@click.option(
    "--version",
    is_flag=True,
    help="Print version and exit.",
)
@click.option(
    "--config-file",
    "-c",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the configuration file to use.",
)
@click.pass_context
def lvmtrurl(ctx: click.Context, config_file: str | None = None, version: bool = False):
    """HAL actor."""

    if version is True:
        click.echo(__version__)
        sys.exit(0)

    default_config_file = os.path.join(os.path.dirname(__file__), "etc/lvmtrurl.yml")

    ctx.obj = {"config_file": config_file or default_config_file}


@lvmtrurl.group(cls=DaemonGroup, prog="trurl_actor", workdir=os.getcwd())
@click.pass_context
@cli_coro()
async def actor(ctx):
    """Runs the actor."""

    config_file = ctx.obj["config_file"]
    print("Configuration file", config_file)

    trurl_actor = TrurlActor.from_config(config_file)

    await trurl_actor.start()
    await trurl_actor.run_forever()


def main():
    lvmtrurl(auto_envvar_prefix="lvmtrurl")


if __name__ == "__main__":
    lvmtrurl()
