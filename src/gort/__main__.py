#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-08
# @Filename: __main__.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

import asyncio

import click

from sdsstools.daemonizer import cli_coro


@click.group()
def gort():
    """Gort CLI."""

    pass


@gort.command()
@cli_coro()
async def overwatcher():
    """Starts the overwatcher."""

    from gort.overwatcher import Overwatcher

    await Overwatcher().run()

    while True:
        await asyncio.sleep(5)
        continue


@gort.command()
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
    open_enclosure: bool = True,
    confirm_open: bool = True,
    focus: bool = True,
):
    """Runs the startup sequence."""

    from gort import Gort

    gort = await Gort(verbosity="debug").init()
    await gort.startup(
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
@click.option(
    "--readout/--no-readout",
    is_flag=True,
    default=False,
    help="Read the spectrographs if an exposure is pending.",
)
@cli_coro()
async def cleanup(readout: bool = False):
    """Runs the cleanup sequence."""

    from gort import Gort

    gort = await Gort(verbosity="debug").init()
    await gort.cleanup(readout=readout)


@gort.command()
@click.argument("RECIPE", type=str)
@cli_coro()
async def recipe(recipe: str):
    """Runs a recipe with its default options."""

    from gort import Gort

    gort = await Gort(verbosity="debug").init()
    await gort.execute_recipe(recipe)


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
@click.option(
    "--only-slew",
    is_flag=True,
    help="Only slews the telescopes but does not take pointing data.",
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
    only_slew: bool = False,
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
        calculate_offset=not only_slew,
    )


def main():
    gort()


if __name__ == "__main__":
    main()
