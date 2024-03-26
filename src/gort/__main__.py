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


@gort.command()
@click.option(
    "--calibrations/--no-calibrations",
    is_flag=True,
    default=False,
    help="Take calibrations as part of the startup sequence.",
)
@click.option(
    "--open-enclosure/--no-open-enclosure",
    is_flag=True,
    default=True,
    help="Open the enclosure.",
)
@click.option(
    "--confirm-open/--no-confirm-open",
    is_flag=True,
    default=True,
    help="Request confirmation before opening the enclosure.",
)
@click.option(
    "--focus/--no-focus",
    is_flag=True,
    default=True,
    help="Focus the telescopes.",
)
@cli_coro()
async def startup(
    calibrations: bool = False,
    open_enclosure: bool = True,
    confirm_open: bool = True,
    focus: bool = True,
):
    """Runs the startup sequence."""

    from gort import Gort

    gort = await Gort(verbosity="debug").init()
    await gort.startup(
        calibration_sequence=calibrations,
        open_enclosure=open_enclosure,
        confirm_open=confirm_open,
        focus=focus,
    )


@gort.command()
@click.option(
    "--park/--no-park",
    is_flag=True,
    default=True,
    help="Parks the telescopes.",
)
@cli_coro()
async def shutdown(park: bool = True):
    """Runs the shutdown sequence."""

    from gort import Gort

    gort = await Gort(verbosity="debug").init()
    await gort.shutdown(park_telescopes=park)


@gort.command()
@cli_coro()
async def focus():
    """Focus the telescopes."""

    from gort import Gort

    gort = await Gort(verbosity="debug").init()
    await gort.guiders.focus()


@gort.command()
@cli_coro()
async def observe():
    """Runs the observe loop."""

    from gort import Gort

    gort = await Gort(verbosity="debug").init()
    await gort.observe(show_progress=True)


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
