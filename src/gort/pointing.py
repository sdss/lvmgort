#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-09
# @Filename: pointing_model.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import pathlib
from functools import partial

from typing import Sequence

import numpy
import polars
from astropy import units as uu
from astropy.coordinates import (
    AltAz,
    EarthLocation,
    SkyCoord,
    uniform_spherical_random_surface,
)
from astropy.time import Time

from gort import Gort


def get_random_sample(
    n_points: int,
    alt_range: tuple[float, float] | None = None,
    az_range: tuple[float, float] | None = None,
):
    """Provides a random sample of RA/Dec points on the surface of a sphere.

    Parameters
    ----------
    n_points
        Number of points to return. This number is ensured even if ``alt_range``
        or ``az_range`` are provided.
    alt_range
        The range of altitude to which to limit the sample, in degrees.
    az_range
        The range of azimuth to which to limit the sample, in degrees.

    Returns
    -------
    coordinates
        A 2D array with the Alt/Az coordinates of the points on the sphere.

    """

    points = numpy.zeros((0, 2), dtype=numpy.float64)

    lco = EarthLocation.of_site("Las Campanas Observatory")
    now = Time.now()

    while True:
        sph_points = uniform_spherical_random_surface(n_points)
        altaz = AltAz(
            alt=sph_points.lat.deg * uu.deg,
            az=sph_points.lon.deg * uu.deg,
            location=lco,
            obstime=now,
        )

        if alt_range is not None:
            alt = altaz.alt.deg
            altaz = altaz[(alt > alt_range[0]) & (alt < alt_range[1])]
        if az_range is not None:
            az = altaz.az.deg
            altaz = altaz[(az > az_range[0]) & (az < az_range[1])]

        altaz = SkyCoord(altaz[altaz.alt.deg > 30])

        altaz_array = numpy.array([altaz.alt.deg, altaz.az.deg], dtype=numpy.float64).T

        points = numpy.vstack((points, altaz_array))

        if points.shape[0] >= n_points:
            return points[0:n_points, :]


async def get_offset(
    gort: Gort,
    telescope: str,
    ra: float,
    dec: float,
    exposure_time: float = 5,
    add_point: bool = True,
):
    """Determines the offset between a pointing an the measured coordinates.

    Parameters
    ----------
    gort
        The instance of `.Gort` used to command the telescope.
    telescope
        The telescope: ``'sci'``, ``'spec'``, etc.
    ra,dec
        The RA/Dec coordinates of the field to observe.
    exposure_time
        The exposure time.
    add_point
        Whether to add the point to the PWI model.

    Returns
    -------
    offset
        A dictionary with the commanded and measured RA/Dec and the offset,
        or `None` if the measurement failed.

    """

    await gort.telescopes[telescope].goto_coordinates(ra=ra, dec=dec)

    replies = await gort.guiders[telescope].actor.commands.guide(
        reply_callback=partial(gort.guiders[telescope].log_replies, skip_debug=False),
        ra=ra,
        dec=dec,
        exposure_time=exposure_time,
        one=True,
        apply_corrections=False,
    )

    replies = replies.flatten()
    if "measured_pointing" not in replies:
        return None

    return_dict = {"commanded_ra": ra, "commanded_dec": dec, "telescope": telescope}
    return_dict["seqno"] = replies["frame"]["seqno"]
    measured_pointing = replies["measured_pointing"]
    return_dict["measured_ra"] = measured_pointing["ra"]
    return_dict["measured_dec"] = measured_pointing["dec"]
    return_dict["offset_ra"] = measured_pointing["radec_offset"][0]
    return_dict["offset_dec"] = measured_pointing["radec_offset"][1]
    return_dict["offset_ax0"] = measured_pointing["motax_offset"][0]
    return_dict["offset_ax1"] = measured_pointing["motax_offset"][1]
    return_dict["separation"] = measured_pointing["separation"]

    if add_point:
        await gort.telescopes[telescope].actor.commands.modelAddPoint(
            measured_pointing["ra"] / 15,
            measured_pointing["dec"],
        )

    return return_dict


async def pointing_model(
    output_file: str | pathlib.Path | None,
    n_points: int,
    alt_range: tuple[float, float],
    az_range: tuple[float, float],
    telescopes: Sequence[str] = ["sci", "spec", "skye", "skyw"],
    calculate_offset: bool = True,
    home: bool = True,
    add_points: bool = True,
) -> polars.DataFrame | None:
    """Iterates over a series of points on the sky measuring offsets.

    Parameters
    ----------
    output_file
        A parquet file where to save the resulting table.
    n_points
        Number of points on the sky to measure.
    alt_range
        The range of altitude to which to limit the sample, in degrees.
    az_range
        The range of azimuth to which to limit the sample, in degrees.
    telescopes
        The list of telescopes to expose.
    calculate_offset
        Determines the offset between the commanded and observed pointings
        by exposing the AG cameras and solving the field.
    home
        Whether to home the telescope before starting.
    add_points
        Add points to the PWI model. Ignored if ``calculate_offset=False``.

    """

    lco = EarthLocation.of_site("Las Campanas Observatory")

    gort = await Gort(verbosity="debug").init()

    points = get_random_sample(n_points, alt_range=alt_range, az_range=az_range)

    data: polars.DataFrame | None = None

    if output_file is not None:
        outputs_dir = pathlib.Path(__file__).parents[2] / "outputs"
        output_file = pathlib.Path(output_file)
        if not output_file.is_absolute():
            output_file = outputs_dir / output_file
        output_file.parent.mkdir(parents=True, exist_ok=True)

        data = polars.read_parquet(str(output_file))

    assert data is None or isinstance(data, polars.DataFrame)

    if home:
        await gort.telescopes.goto_named_position("zenith")
        await gort.telescopes.home()

    for npoint, (alt, az) in enumerate(points):
        print()

        altaz = SkyCoord(
            AltAz(
                alt=alt * uu.deg,
                az=az * uu.deg,
                obstime=Time.now(),
                location=lco,
            )
        )
        icrs = altaz.transform_to("icrs")
        ra = icrs.ra.deg
        dec = icrs.dec.deg

        gort.log.info(f"({npoint+1}/{n_points}): Going to {ra:.6f}, {dec:.6f}.")

        if calculate_offset is False:
            tasks = [
                gort.telescopes[telescope].goto_coordinates(ra=ra, dec=dec)
                for telescope in telescopes
            ]
            await asyncio.gather(*tasks)
            await asyncio.sleep(3)

            continue

        results = await asyncio.gather(
            *[
                get_offset(gort, tel, ra, dec, add_point=add_points)
                for tel in telescopes
            ],
            return_exceptions=True,
        )

        valid = []
        for ii, tel in enumerate(telescopes):
            result = results[ii]
            if result is None:
                gort.log.warning(
                    f"Failed determining offset for telescope {tel} "
                    f"at ({ra:.6f}, {dec:.6f})"
                )
            elif isinstance(result, BaseException):
                gort.log.warning(f"Telescope {tel} failed with error: {str(result)}")
            else:
                valid.append(result)

        if len(valid) == 0:
            continue

        this_offsets = polars.DataFrame(valid)

        print()
        print(this_offsets)

        if data is not None:
            data = polars.concat([data, this_offsets])
        else:
            data = this_offsets

        if output_file:
            data.write_parquet(str(output_file))

    return data


if __name__ == "__main__":
    NPOINTS = 50
    ALT_RANGE = (40, 88)
    AZ_RANGE = (0, 355)
    TELESCOPES = ["sci", "spec", "skye", "skyw"]

    OUTPUT = "pointing_model_sci_10points_add.h5"

    asyncio.run(
        pointing_model(
            OUTPUT,
            NPOINTS,
            ALT_RANGE,
            AZ_RANGE,
            telescopes=TELESCOPES,
            home=True,
            add_points=True,
        )
    )
