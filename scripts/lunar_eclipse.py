#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2025-03-02
# @Filename: lunar_eclipse.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import pathlib

from typing import Annotated

import numpy
import typer
from astropy.coordinates import EarthLocation, get_body
from astropy.time import Time

from sdsstools.daemonizer import cli_coro
from sdsstools.utils import GatheringTaskGroup

from gort import Gort, Tile
from gort.tools import cancel_task


STOP_FILE_PATH = pathlib.Path("/tmp/stop_lunar_eclipse")


async def monitor_sci(gort: Gort):
    """Guides with the sci telescope but does not apply corrections."""

    gort.log.debug("Starting to guide with the science telescope (no corrections).")
    await gort.guiders.sci.guide(monitor=False, apply_corrections=False)
    gort.log.debug("Guiding with the science telescope finished.")


async def track_moon_with_sci(gort: Gort):
    """Tracks the Moon with the science telescope."""

    gort.log.debug("Starting to track the Moon with the science telescope.")

    lco = EarthLocation.of_site("Las Campanas Observatory")
    now = Time.now()

    moon = get_body("moon", now, location=lco)  # In GCRS coordinates

    moon_ra = moon.icrs.ra.deg.item()
    moon_dec = moon.icrs.dec.deg.item()
    gort.log.info(f"Moon coordinates: RA={moon_ra:.6f}, Dec={moon_dec:.6f}")

    while True:
        await asyncio.sleep(5)

        now = Time.now()
        moon = get_body("moon", now, location=lco)

        new_moon_ra = moon.icrs.ra.deg.item()
        new_moon_dec = moon.icrs.dec.deg.item()
        gort.log.info(f"Moon coordinates: RA={new_moon_ra:.6f}, Dec={new_moon_dec:.6f}")

        off_dec = new_moon_dec - moon_dec
        off_ra = (new_moon_ra - moon_ra) * numpy.cos(numpy.radians(new_moon_dec))

        gort.log.info(f"Offset: RA={off_ra * 3600:.1f}, Dec={off_dec * 3600:.1f}")

        await gort.telescopes.sci.offset(off_ra * 3600, off_dec * 3600)

        moon_ra = new_moon_ra
        moon_dec = new_moon_dec


async def lunar_eclipse(
    gort: Gort,
    exposure_time: float,
    slew_sci: bool = True,
    track_sci: bool = False,
    only_sci: bool = False,
    dark: bool = False,
    continuous: bool = False,
    close_hartmann: bool = False,
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

    observer = gort.observer

    sci_monitor_task: asyncio.Task | None = None
    track_moon_task: asyncio.Task | None = None

    if close_hartmann:
        async with GatheringTaskGroup() as group:
            group.create_task(gort.specs["sp1"].ieb.close("hartmann_left"))
            group.create_task(gort.specs["sp2"].ieb.close("hartmann_left"))
            group.create_task(gort.specs["sp3"].ieb.close("hartmann_left"))
    else:
        await gort.specs.reset(full=True)

    is_acquired: bool = False

    while True:
        observer.reset(tile, reset_stages=not is_acquired)

        if not is_acquired:
            if exposure_time < 300:
                gort.log.warning("Exposure time is too short. Using only sci.")
                only_sci = True

            if only_sci:
                if slew_sci:
                    await observer.slew(telescopes=["sci"])
                    sci_monitor_task = asyncio.create_task(monitor_sci(gort))

                if track_sci:
                    track_moon_task = asyncio.create_task(track_moon_with_sci(gort))

            else:
                if slew_sci:
                    await observer.slew()
                else:
                    await observer.slew(telescopes=["spec", "skye", "skyw"])

                sci_monitor_task = asyncio.create_task(monitor_sci(gort))
                await observer.acquire(telescopes=["spec", "skye", "skyw"])

                if track_sci:
                    track_moon_task = asyncio.create_task(track_moon_with_sci(gort))

        else:
            if not only_sci and observer.standards:
                await observer.standards.reacquire_first()

            observer.guider_monitor.reset()

        is_acquired = True
        await observer.expose(
            exposure_time=exposure_time,
            object="lunar_eclipse_2025",
            show_progress=True,
            keep_guiding=True,
            async_readout=False,
        )
        await observer.finish_observation(keep_guiding=continuous)

        stop_file_exists = STOP_FILE_PATH.exists()
        STOP_FILE_PATH.unlink(missing_ok=True)

        if continuous and not stop_file_exists:
            continue

        await cancel_task(sci_monitor_task)
        await cancel_task(track_moon_task)

        await gort.guiders.stop()

        if dark:
            await gort.specs.expose(
                exposure_time,
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
    exposure_time: Annotated[
        float,
        typer.Argument(
            metavar="EXP_TIME",
            min=0.0,
            max=900,
            help="Science exposure time",
        ),
    ],
    continuous: Annotated[
        bool,
        typer.Option("-c", "--continuous", help="Continuous mode"),
    ] = False,
    only_sci: Annotated[
        bool,
        typer.Option("--only-sci", help="Only use the science telescope"),
    ] = False,
    slew_sci: Annotated[
        bool | None,
        typer.Option("--slew-sci/--no-slew-sci", help="Slew sci to the Moon"),
    ] = None,
    track_sci: Annotated[
        bool,
        typer.Option("--track-sci/--no-track-sci", help="Track the Moon"),
    ] = False,
    dark: Annotated[
        bool,
        typer.Option("--dark", help="Take dark frames"),
    ] = False,
    close_hartmann: Annotated[
        bool,
        typer.Option("--close-hartmann", help="Close the left Hartmann door"),
    ] = False,
):
    """Lunar eclipse 2025 observing."""

    if slew_sci is None:
        slew_sci = track_sci

    gort = await Gort(override_overwatcher=True, verbosity="debug").init()

    await lunar_eclipse(
        gort=gort,
        exposure_time=exposure_time,
        slew_sci=slew_sci,
        track_sci=track_sci,
        only_sci=only_sci,
        dark=dark,
        continuous=continuous,
        close_hartmann=close_hartmann,
    )


if __name__ == "__main__":
    cli()
