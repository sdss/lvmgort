#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2025-03-02
# @Filename: lunar_eclipse.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from functools import partial

from typing import Annotated

import typer
from astropy.coordinates import EarthLocation, get_body
from astropy.time import Time

from sdsstools.daemonizer import cli_coro

from gort import Gort, Tile
from gort.tools import cancel_task


async def guide_with_sci(gort: Gort):
    """Guides with the sci telescope but does not apply corrections."""

    gort.log.debug("Starting to guide with the science telescope (no corrections).")
    await gort.guiders.sci.guide(monitor=False, apply_corrections=False)
    gort.log.debug("Guiding with the science telescope finished.")


async def park_sci(gort: Gort, science_exptime: float):
    """Parks the science telescope after the science exposure time."""

    await asyncio.sleep(science_exptime)

    gort.log.warning("Parking the science telescope.")

    await asyncio.gather(
        gort.telescopes.sci.park(kmirror=False, disable=False),
        gort.guiders.sci.stop(),
    )

    gort.log.warning("Science telescope parked.")


async def lunar_eclipse(
    gort: Gort,
    science_exptime: float,
    exptime: float | None = None,
    only_sci: bool = False,
    dark: bool = False,
):
    """Observes a lunar eclipse."""

    gort.log.info("Starting lunar eclipse observation.")

    lco = EarthLocation.of_site("Las Campanas Observatory")
    now = Time.now()

    moon = get_body("moon", now, location=lco)  # In GCRS coordinates
    moon_ra = moon.icrs.ra.deg.item()
    moon_dec = moon.icrs.dec.deg.item()

    gort.log.info(f"Moon coordinates: RA={moon_ra:.6f}, Dec={moon_dec:.6f}")

    # Create a tile with the current Moon centre coordinates.
    tile = Tile.from_coordinates(moon_ra, moon_dec)

    if only_sci:
        tile.set_spec_coords()
        tile.set_sky_coords()
        exptime = science_exptime

    # Default to using the same exposure time for all
    # telescopes if exptime not provided.
    exptime = exptime or science_exptime
    move_sci = exptime > science_exptime

    observer = gort.observer
    observer.reset(tile=tile)

    # Slew all telescopes to their initial positions
    await observer.slew()

    # Guide with all telescopes except sci, but do take guide frames
    # without corrections with the sci telescope.
    sci_guiding_task = asyncio.create_task(guide_with_sci(gort))

    if not only_sci:
        await observer.acquire(telescopes=["spec", "skye", "skyw"])

    exposure_starts_callback = partial(park_sci, gort, science_exptime)
    await observer.expose(
        exposure_time=exptime,
        object="lunar_eclipse_2025",
        show_progress=True,
        keep_guiding=True,
        async_readout=False,
        exposure_starts_callback=exposure_starts_callback if move_sci else None,  # type: ignore
    )

    await observer.finish_observation()
    await cancel_task(sci_guiding_task)

    if dark:
        await gort.guiders.stop()
        if move_sci:
            await gort.telescopes.sci.goto_coordinates(
                ra=tile.sci_coords.ra,
                dec=tile.sci_coords.dec,
            )
        await gort.specs.expose(
            science_exptime,
            flavour="dark",
            object="lunar_eclipse_2025",
        )


cli = typer.Typer(
    name="lunar_eclipse",
    rich_markup_mode="rich",
    context_settings={"obj": {}},
    no_args_is_help=True,
    help="Lunar eclipse March 2025 observing.",
)


@cli.command()
@cli_coro()
async def lunar_eclipse_cli(
    science_exposure_time: Annotated[
        float,
        typer.Argument(
            metavar="SCI_EXP_TIME",
            min=0.0,
            max=900,
            help="Science exposure time",
        ),
    ],
    continuous: Annotated[
        bool,
        typer.Option("-c", "--continuous", help="Continuous mode"),
    ] = False,
    exposure_time: Annotated[
        float | None,
        typer.Option("-e", "--exposure-time", help="Exposure time"),
    ] = None,
    only_sci: Annotated[
        bool,
        typer.Option("--only-sci", help="Only guide with the science telescope"),
    ] = False,
    with_dark: Annotated[
        bool,
        typer.Option("--dark", help="Take dark frames"),
    ] = False,
):
    """Lunar eclipse 2025 observing."""

    if not only_sci and science_exposure_time < 60:
        raise typer.Abort(
            "Science exposure time must be at least "
            "60 seconds when using all telescopes."
        )

    gort = await Gort(override_overwatcher=True, verbosity="debug").init()

    while True:
        await lunar_eclipse(
            gort=gort,
            science_exptime=science_exposure_time,
            exptime=exposure_time,
            only_sci=only_sci,
            dark=with_dark,
        )

        if not continuous:
            break


if __name__ == "__main__":
    cli()
