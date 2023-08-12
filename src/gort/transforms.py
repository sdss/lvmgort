#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-11
# @Filename: transforms.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import pathlib
import re

import numpy
import pandas
import yaml

from gort import config


__all__ = [
    "read_fibermap",
    "offset_to_master_frame_pixel",
    "xy_to_radec_offset",
    "fibre_slew_coordinates",
    "radec_sexagesimal_to_decimal",
    "fibre_to_master_frame",
]


_FIBERMAP_CACHE: pandas.DataFrame | None = None


def read_fibermap(
    path: str | pathlib.Path | None = None,
    force_cache: bool = False,
) -> pandas.DataFrame:
    """Reads the fibermap file.

    Parameters
    ----------
    path
        Path to the fibermap file. If `None` uses the path from the
        configuration file.
    force_cache
        If `True`, forces a re-read of the fibermap file; otherwise reads it
        from the cache if available.

    Returns
    -------
    fibermap
        A pandas DataFrame with the fibermap data.

    """

    global _FIBERMAP_CACHE

    if path is None:
        path = pathlib.Path(config["lvmcore"]["path"]) / config["lvmcore"]["fibermap"]
    else:
        path = pathlib.Path(path).absolute()

    assert isinstance(path, pathlib.Path)

    if not path.exists():
        raise FileNotFoundError(f"Fibermap file {str(path)} does not exist.")

    if force_cache is False and _FIBERMAP_CACHE is not None:
        return _FIBERMAP_CACHE

    fibermap_y = yaml.load(open(str(path)), Loader=yaml.CFullLoader)

    schema = fibermap_y["schema"]
    cols = [it["name"] for it in schema]
    dtypes = [it["dtype"] if it["dtype"] != "str" else "<U8" for it in schema]

    fibers = pandas.DataFrame.from_records(
        numpy.array(
            [tuple(fibs) for fibs in fibermap_y["fibers"]],
            dtype=list(zip(cols, dtypes)),
        ),
    )

    # Lower-case some columns.
    for col in fibers:
        is_str = pandas.api.types.is_string_dtype(fibers[col].dtype)
        if is_str and col in ["targettype", "telescope"]:
            fibers[col] = fibers[col].str.lower()

    # Add a new column with the full name of the fibre, as ifulabel-finifu
    fibers["fibername"] = fibers["orig_ifulabel"]

    _FIBERMAP_CACHE = fibers

    return _FIBERMAP_CACHE


def offset_to_master_frame_pixel(
    xmm: float | None = None,
    ymm: float | None = None,
    ra: float | None = None,
    dec: float | None = None,
) -> tuple[float, float]:
    """Determines the pixel on master frame coordinates for an offset.

    Parameters
    ----------
    xmm
        The x offset, in mm, with respect to the central fibre in the IFU.
    ymm
        The y offset, in mm, with respect to the central fibre in the IFU.
    ra
        The offset in RA, in arcsec. See :obj:`xy_to_radec_offset` for the
        caveats on this calculations.
    dec
        The offset in declination, in arcsec.


    Returns
    -------
    pixel
        A tuple with the x and z coordinates of the pixel in the master frame.

    Raises
    ------
    ValueError
        If the pixel is out of bounds.

    """

    PIXEL_SIZE = 9  # microns / pixel
    PIXEL_SCALE = 1  # arcsec/pixel

    XZ_0 = (2500, 1000)  # Central pixel in the master frame

    if xmm is not None and ymm is not None:
        if ra is not None or dec is not None:
            raise ValueError("ra/dec cannot be set along with xmm/ymm.")

    elif ra is not None and dec is not None:
        if xmm is not None or ymm is not None:
            raise ValueError("xmm/ymm cannot be set along with ra/dec.")

        xmm = ra * PIXEL_SIZE / PIXEL_SCALE / 1000
        ymm = -dec * PIXEL_SIZE / PIXEL_SCALE / 1000

    else:
        raise ValueError("Not enough inputs supplied.")

    x_mf = xmm * 1000 / PIXEL_SIZE + XZ_0[0]
    y_mf = ymm * 1000 / PIXEL_SIZE + XZ_0[1]

    if x_mf < 0 or x_mf > 2 * XZ_0[0] or y_mf < 0 or y_mf > 2 * XZ_0[1]:
        raise ValueError("Pixel is out of bounds.")

    return (round(x_mf, 1), round(y_mf, 1))


def xy_to_radec_offset(xpmm: float, ypmm: float):
    """Converts offsets in the IFU to approximate RA/Dec offsets.

    .. warning::
        This is an approximate conversion that assumes the IFU is perfectly
        aligned with the AG cameras in the focal plane and that the field
        de-rotation is perfect. It should only be used to determine initial
        offsets for blind telescope slews.

    Parameters
    ----------
    xpmm
        The x offset with respect to the central IFU fibre, in mm, as defined
        in the fibermap.
    ypmm
        As ``xpmm`` for the y offset.

    Returns
    -------
    radec
        RA/Dec offset in arcsec as a tuple. See the warning above for caveats.

    """

    # In the master frame / focal plane, increase x means increasing RA and
    # increasing y means decreasing Dec (i.e., axes are rotated 180 degrees
    # wrt the usual North up, East left).

    PIXEL_SIZE = 9  # microns/pixel
    PIXEL_SCALE = 1  # arcsec/pixel

    x_arcsec = xpmm * 1000 / PIXEL_SIZE * PIXEL_SCALE
    y_arcsec = ypmm * 1000 / PIXEL_SIZE * PIXEL_SCALE

    return (x_arcsec, -y_arcsec)


def fibre_slew_coordinates(
    ra: float,
    dec: float,
    fibre_name: str,
) -> tuple[float, float]:
    """Determines the slew coordinates for a fibre.

    This function provides approximate slew coordinates to place
    a target with coordinates ``ra``, ``dec`` on a given fibre. The
    preferred way to use it is to slew the telescope using the
    coordinates returned by this function but then guide on the
    original target coordinates and use the appropriate pixel on the
    master frame to guide on the fibre.

    Parameters
    ----------
    ra,dec
        The RA and Dec of the target to which to slew.
    fibre_name
        The fibre to which to slew the target, with the format
        ``<ifulabel>-<finifu>``.

    Returns
    -------
    slew_coordinates
        A tuple of RA/Dec coordinates to slew, which should place
        the target near the desired fibre.

    """

    fibermap = read_fibermap()

    if fibre_name not in fibermap.fibername.values:
        raise NameError(f"Fibre {fibre_name} not found in fibermap.")

    fibre = fibermap.loc[fibermap.fibername == fibre_name, :]
    xpmm, ypmm = fibre.loc[:, ["xpmm", "ypmm"]].values[0]

    ra_off, dec_off = xy_to_radec_offset(xpmm, ypmm)

    ra_slew = ra + ra_off / 3600.0 / numpy.cos(numpy.radians(dec))
    dec_slew = dec + dec_off / 3600.0

    return (ra_slew, dec_slew)


def radec_sexagesimal_to_decimal(ra: str, dec: str, ra_is_hours: bool = True):
    """Converts a string of sexagesimal RA and Dec to decimal."""

    ra_match = re.match(r"([+-]?\d+?)[:hd\s](\d+)[:m\s](\d*\.?\d*)", ra)
    if ra_match is None:
        raise ValueError("Invalid format for RA.")

    ra_groups = ra_match.groups()
    ra_deg = float(ra_groups[0]) + float(ra_groups[1]) / 60 + float(ra_groups[2]) / 3600

    if ra_is_hours:
        ra_deg *= 15

    dec_match = re.match(r"([+-]?\d+?)[:hd\s](\d+)[:m\s](\d*\.?\d*)", dec)
    if dec_match is None:
        raise ValueError("Invalid format for Dec.")

    dec_groups = dec_match.groups()
    dec_deg = float(dec_groups[0])

    if dec_deg >= 0:
        dec_deg += float(dec_groups[1]) / 60 + float(dec_groups[2]) / 3600
    else:
        dec_deg -= float(dec_groups[1]) / 60 - float(dec_groups[2]) / 3600

    return ra_deg, dec_deg


def fibre_to_master_frame(fibre_name: str):
    """Returns the xz coordinates in the master frame of a named fibres.

    Parameters
    ----------
    fibre_name
        The fibre for which to calculate the master frame coordinates,
        with the format ``<ifulabel>-<finifu>``.

    Returns
    -------
    pixel
        A tuple with the x and z coordinates of the pixel in the master frame.

    Raises
    ------
    ValueError
        If the pixel is out of bounds.

    """

    XZ_0 = (2500, 1000)  # Central pixel in the master frame

    fibermap = read_fibermap()

    if fibre_name not in fibermap.fibername.values:
        raise NameError(f"Fibre {fibre_name} not found in fibermap.")

    fibre = fibermap.loc[fibermap.fibername == fibre_name, :]
    xpmm, ypmm = fibre.loc[:, ["xpmm", "ypmm"]].values[0]

    x_mf, z_mf = offset_to_master_frame_pixel(xmm=xpmm, ymm=ypmm)

    if x_mf < 0 or x_mf > 2 * XZ_0[0] or z_mf < 0 or z_mf > 2 * XZ_0[1]:
        raise ValueError("Pixel is out of bounds.")

    return (round(x_mf, 1), round(z_mf, 1))
