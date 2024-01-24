#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-08
# @Filename: __main__.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

import os

import click

from sdsstools.daemonizer import DaemonGroup, cli_coro


@click.group()
def gort():
    """Gort CLI."""

    pass


@gort.group(cls=DaemonGroup, prog="gort_ws", workdir=os.getcwd())
@cli_coro()
async def websocket():
    """Launches the websocket server."""

    from gort.websocket import WebsocketServer

    ws = WebsocketServer()
    await ws.start()

    await ws.websocket_server.serve_forever()


@gort.command(name="pointing-model")
@click.argument(
    "TELESCOPES",
    type=click.Choice(["sci", "spec", "skye", "skyw"]),
    nargs=-1,
    required=False,
)
@click.option(
    "-n",
    "--n-points",
    default=50,
    help="Number of points to sample.",
)
@click.option(
    "-a",
    "--alt-range",
    nargs=2,
    type=float,
    default=(40, 85),
    help="Altitude range.",
)
@click.option(
    "-z",
    "--az-range",
    nargs=2,
    type=float,
    default=(0, 355),
    help="Azimuth range.",
)
@click.option(
    "-f",
    "--filename",
    type=click.Path(exists=False, dir_okay=False),
    help="Path where to save the pointing data.",
)
@click.option(
    "-m",
    "--home",
    is_flag=True,
    help="Home the telescope before acquiring data.",
)
@click.option(
    "-P/-p",
    "--add-points/--no-add-points",
    is_flag=True,
    default=True,
    help="Add points to the PWI model.",
)
@cli_coro()
async def pointing_model(
    telescopes: tuple[str, ...] | None,
    alt_range: tuple[float, float] = (40, 85),
    az_range: tuple[float, float] = (0, 355),
    filename: str | None = None,
    n_points: int = 50,
    home: bool = False,
    add_points: bool = True,
):
    """Acquires a pointing model."""

    from gort.pointing import pointing_model

    if telescopes is None or len(telescopes) == 0:
        telescopes = ("sci", "spec", "skye", "skyw")

    await pointing_model(
        filename,
        n_points,
        alt_range,
        az_range,
        telescopes=telescopes,
        home=home,
        add_points=add_points,
    )


def main():
    gort()


if __name__ == "__main__":
    main()
