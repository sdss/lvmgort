#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-11
# @Filename: transforms.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import math
import pathlib
import re

import astropy.coordinates
import astropy.time
import astropy.units
import numpy
import polars
import yaml
from astropy.coordinates import Angle

from gort import config


__all__ = [
    "read_fibermap",
    "offset_to_master_frame_pixel",
    "xy_to_radec_offset",
    "fibre_slew_coordinates",
    "radec_sexagesimal_to_decimal",
    "fibre_to_master_frame",
    "calculate_position_angle",
    "calculate_field_angle",
    "Siderostat",
    "HomTrans",
    "Mirror",
    "wrap_pa_hex",
]


_FIBERMAP_CACHE: polars.DataFrame | None = None


def read_fibermap(
    path: str | pathlib.Path | None = None,
    force_cache: bool = False,
) -> polars.DataFrame:
    """Reads the fibermap file.

    Parameters
    ----------
    path
        Path to the fibermap file. If :obj:`None` uses the path from the
        configuration file.
    force_cache
        If :obj:`True`, forces a re-read of the fibermap file; otherwise reads it
        from the cache if available.

    Returns
    -------
    fibermap
        A Polars DataFrame with the fibermap data.

    """

    global _FIBERMAP_CACHE

    if path is None:
        lvmcore_config = config["services"]["lvmcore"]
        path = pathlib.Path(lvmcore_config["path"]) / lvmcore_config["fibermap"]
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

    fibers = polars.from_numpy(
        numpy.array(
            [tuple(fibs) for fibs in fibermap_y["fibers"]],
            dtype=list(zip(cols, dtypes)),
        ),
    )

    # Lower-case some columns.
    fibers = fibers.with_columns(
        polars.col("targettype", "telescope").str.to_lowercase(),
        fibername=polars.col.orig_ifulabel,
    )

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
    derotated: bool = True,
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
    derotated
        Whether a k-mirror is derotating the field. If `False`, the
        field rotation for the current time will be used.

    Returns
    -------
    slew_coordinates
        A tuple of RA/Dec coordinates to slew, which should place
        the target near the desired fibre.

    """

    fibermap = read_fibermap()

    if fibre_name not in fibermap["fibername"]:
        raise NameError(f"Fibre {fibre_name} not found in fibermap.")

    fibre = fibermap.row(named=True, by_predicate=polars.col.fibername == fibre_name)

    ra_off, dec_off = xy_to_radec_offset(fibre["xpmm"], fibre["ypmm"])

    if not derotated:
        field_angle = calculate_field_angle(ra, dec, obstime=None)

        # We rotate by -fa_r. I.e., we are "derotating" the sky then
        # calculating the offsets.
        fa_r = -numpy.radians(field_angle)
        rotm = numpy.array(
            [
                [numpy.cos(fa_r), -numpy.sin(fa_r)],
                [numpy.sin(fa_r), numpy.cos(fa_r)],
            ]
        )
        ra_off, dec_off = rotm.dot(numpy.array([ra_off, dec_off]).T)

        # Account for the fact that when we apply the rotation the axes are
        # 180 deg off.
        dec_off = -dec_off

    ra_off = ra_off / 3600.0 / numpy.cos(numpy.radians(dec))
    dec_off = dec_off / 3600.0

    return ra + ra_off, dec + dec_off


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

    if fibre_name not in fibermap["fibername"]:
        raise NameError(f"Fibre {fibre_name} not found in fibermap.")

    fibre = fibermap.row(named=True, by_predicate=polars.col.fibername == fibre_name)
    xpmm = fibre["xpmm"]
    ypmm = fibre["ypmm"]

    x_mf, z_mf = offset_to_master_frame_pixel(xmm=xpmm, ymm=ypmm)

    if x_mf < 0 or x_mf > 2 * XZ_0[0] or z_mf < 0 or z_mf > 2 * XZ_0[1]:
        raise ValueError("Pixel is out of bounds.")

    return (round(x_mf, 1), round(z_mf, 1))


def calculate_position_angle(ra: float, dec: float, obstime: astropy.time.Time | str):
    """Calculates the position angle seen for a set of coordinates.

    Parameters
    ---------
    ra
        The RA of the centre of the field.
    dec
        The Dec of the centre of the field.
    obstime
        The time of the observation. Either an ISOT string or an astropy time
        object.

    Returns
    -------
    ph
        The position angle. In an image this is the angle between North and the
        y direction, with positive angles being CCW.

    """

    site = astropy.coordinates.EarthLocation.from_geodetic(**config["site"])

    if isinstance(obstime, str):
        obstime_ap = astropy.time.Time(obstime, format="isot")
    else:
        obstime_ap = obstime

    assert isinstance(obstime, astropy.time.Time)

    obstime_ap.location = site

    lst = obstime_ap.sidereal_time("mean")

    ha = lst.deg - ra

    ha_r = numpy.radians(ha)
    lat_r = numpy.radians(site.lat.deg)
    dec_r = numpy.radians(dec)

    par = numpy.arctan2(
        numpy.sin(ha_r),
        numpy.cos(dec_r) * numpy.tan(lat_r) - numpy.sin(dec_r) * numpy.cos(ha_r),
    )

    return numpy.degrees(par)


def calculate_field_angle(
    ra: float,
    dec: float,
    obstime: str | astropy.time.Time | None = None,
):
    """Returns the field angle for a set of coordinates.

    Parameters
    ---------
    ra
        The RA of the centre of the field.
    dec
        The Dec of the centre of the field.
    obstime
        The time of the observation. Either an ISOT string or an astropy time
        object.

    Returns
    -------
    fa
        The field angle. See `.Siderostat.field_angle` for details.

    """

    if isinstance(obstime, str):
        obstime = astropy.time.Time(obstime, format="isot")
    elif obstime is None:
        obstime = astropy.time.Time.now()

    assert isinstance(obstime, astropy.time.Time)

    siderostat = Siderostat()
    target = astropy.coordinates.SkyCoord(ra=ra, dec=dec, unit="deg", frame="icrs")

    return siderostat.field_angle(target, time=obstime)


def wrap_pa_hex(pa: float):
    """Wraps a position angle to the range -30 to 30 degrees.

    Parameters
    ----------
    pa
        The position angle to wrap. Always in degrees in the range 0 to infinity.

    Returns
    -------
    pa_wrap
        The wrapped position angle in the range -30 to 30 degrees.

    """

    # First convert to the range 0 to 360 degrees.
    pa = pa % 360

    # Then wrap to the range 0 to 60 degrees to account for the IFU hexagonal symmetry.
    pa = pa % 60

    # Finally, wrap to the range -30 to 30 degrees.
    if pa > 30:
        pa -= 60

    return pa


class Siderostat:
    """A siderostat of 2 mirrors.

    Adapted from https://github.com/sdss/lvmtipo/blob/main/python/lvmtipo/siderostat.py

    Parameters
    ----------

    zenang
        Zenith angle of the direction of the exit beam (degrees) in the range
        0..180. Default is the design value of the LVMT: horizontal
    azang
        Azimuth angle of the direction of the exit beam (degrees) in the range
        -180..360 degrees, N=0, E=90.
        Ought to be zero for the LCO LVMT where the FP is north of the
        siderostat and 180 for the MPIA test setup where the FP is
        south of the siderostat. The default is the angle for LCO.
    medSign
        Sign of the meridian flip design of the mechanics.
        Must be either +1 or -1. Default is the LCO LVMT design as build (in newer
        but not the older documentation).
    m1m2dist
        Distance between the centers of M1 and M2 in millimeter.
        The default value is taken from ``LVM-0098_Sky Baffle Design``
        of 2022-04-18 by subracting the 84 and 60 cm distance of the
        output pupils to M1 and M2.
    om2_off_ang
        The offset angle which aligns PW motor angle of the M2 axis,
        which is ax0 of the PW GUI, to the angles of the manuscript.
    om1_off_ang
        The offset angle which aligns PW motor angle of the M1 axis,
        which is ax1 of the PW GUI, to the angles of the manuscript, degrees.
        ``om1_off_ang`` and ``om2_off_ang`` are either angles in degrees or
        both ``astropy.coordinates.Angle``.

    """

    def __init__(
        self,
        zenang: float = 90.0,
        azang: float = 0.0,
        medSign: int = -1,
        m1m2dist: float = 240.0,
        om1_off_ang: float | Angle = 118.969,
        om2_off_ang: float | Angle = -169.752,
    ):
        # the vector b[0..2] is the three cartesian coordinates
        # of the beam after leaving M2 in the topocentric horizontal system.
        # b[0] is the coordinate along E, b[1] along N and b[2] up.
        if isinstance(zenang, (int, float)) and isinstance(azang, (int, float)):
            self.b = numpy.zeros((3))
            self.b[0] = math.sin(math.radians(azang)) * math.sin(math.radians(zenang))
            self.b[1] = math.cos(math.radians(azang)) * math.sin(math.radians(zenang))
            self.b[2] = math.cos(math.radians(zenang))
        else:
            raise TypeError("Invalid data types.")

        self.m1m2len = m1m2dist

        if isinstance(medSign, int):
            if medSign in (1, -1):
                self.sign = medSign
            else:
                raise ValueError("Invalid medSign value.")
        else:
            raise TypeError("Invalid medSign data type.")

        # axes orthogonal to beam. box points essentially to the zenith
        # and boy essentially to the East (at LCO) resp West (at MPIA)
        self.box = numpy.zeros((3))
        self.box[0] = 0.0
        self.box[1] = -self.b[2]
        self.box[2] = self.b[1]
        self.boy = numpy.cross(self.b, self.box)

        # The 3x3 B-matrix converts a (x,y,z) vector on the unit spehre
        # which is a direction from the observer to the star in alt-az (horizontal)
        # coordinates (x points East, y to the North and z to the zenith)
        # into a vector on the unit sphere where the two PW motor angles (and offsets)
        # play the role of the azimuth and polar angles.
        # See https://www.mpia.de/~mathar/public/mathar20201208.pdf

        bproj = math.hypot(self.b[1], self.b[2])  # sqrt(by^2+bz^2)

        self.B = numpy.array([[0, 0, 0], [0, 0, 0], [0, 0, 0]], dtype=numpy.double)
        self.B[0][0] = -self.sign * bproj
        self.B[0][1] = self.sign * self.b[0] * self.b[1] / bproj
        self.B[0][2] = self.sign * self.b[0] * self.b[2] / bproj
        self.B[1][0] = 0
        self.B[1][1] = -self.sign * self.b[2] / bproj
        self.B[1][2] = self.sign * self.b[1] / bproj
        self.B[2][0] = self.b[0]
        self.B[2][1] = self.b[1]
        self.B[2][2] = self.b[2]

        if isinstance(om1_off_ang, Angle) and isinstance(om2_off_ang, Angle):
            self.pw_ax_off = [om1_off_ang.radian, om2_off_ang.radian]  # type: ignore
        else:
            self.pw_ax_off = [math.radians(om1_off_ang), math.radians(om2_off_ang)]

    def field_angle(
        self,
        target: astropy.coordinates.SkyCoord,
        time: astropy.time.Time | str | None = None,
    ):
        """Determines the field angle (direction to NCP).

        Parameters
        ----------
        target
            Sidereal target in ra/dec.
        time
            Time of the observation.

        Returns
        -------
        field_angle
            Field angle (direction to NCP) in degrees.

        """

        if isinstance(time, astropy.time.Time):
            pass
        elif isinstance(time, str):
            time = astropy.time.Time(time, format="isot", scale="utc")
        elif time is None:
            time = astropy.time.Time.now()

        # Compute mirror positions
        site = astropy.coordinates.EarthLocation.from_geodetic(**config["site"])

        assert isinstance(astropy.units.Pa, astropy.units.Unit)
        assert isinstance(astropy.units.um, astropy.units.Unit)

        pres = 101300 * math.exp(-site.height.value / 8135.0) * astropy.units.Pa

        altaz = astropy.coordinates.AltAz(
            location=site,
            obstime=time,
            pressure=pres,
            temperature=15 * astropy.units.deg_C,
            relative_humidity=0.5,
            obswl=0.5 * astropy.units.um,
        )

        horiz = target.transform_to(altaz)

        assert isinstance(horiz.az, astropy.coordinates.Angle)
        assert isinstance(horiz.alt, astropy.coordinates.Angle)

        star = numpy.zeros((3))

        # Same procedure as in the construction of b in the Sider ctor,
        # but with 90-zenang=alt
        star[0] = math.sin(horiz.az.radian) * math.cos(horiz.alt.radian)
        star[1] = math.cos(horiz.az.radian) * math.cos(horiz.alt.radian)
        star[2] = math.sin(horiz.alt.radian)

        # unit vector from M2 to M1
        # we're not normalizing to self.m1m2len but keeping the vector
        # m2tom1 at length 1 to simplify the later subtractions to compute
        # normal vectors from other unit vector
        m2tom1 = numpy.cross(star, self.b)
        vlen = numpy.linalg.norm(m2tom1)
        m2tom1 /= self.sign * float(vlen)

        # surface normal to M1 (not normalized to 1)
        m1norm = star - m2tom1
        # the orthogonal distance of the points of M1 to the origin
        # of coordinates are implied by the m1norm direction and
        # the fact that m2tom1 is on the surface. So the homogeneous
        # coordinate equation applied to m2tom1 should yield m2tom1 itself.
        # This requires (m2tom1 . m1norm -d) * n1morm=0 where dots are dot products.
        # the latter dot product actually requires a normalized m1norm
        vlen = numpy.linalg.norm(m1norm)
        m1norm /= vlen
        m1 = Mirror(m1norm, numpy.dot(m2tom1, m1norm))

        # surface normal to M2 (not normalized to 1)
        m2norm = self.b + m2tom1
        m2 = Mirror(m2norm, 0.0)

        # transformation matrix for the 2 reflections individually and in series
        m1trans = m1.to_hom_trans()
        m2trans = m2.to_hom_trans()
        trans = m2trans.multiply(m1trans)

        # for the field angle need a target that is just a little bit
        # more north (but not too little to avoid loss of precision)
        # 10 arcmin = 0.16 deg further to NCP
        targNcp = target.spherical_offsets_by(Angle("0deg"), Angle("0.16deg"))
        horizNcp = targNcp.transform_to(altaz)

        starNcp = numpy.zeros((3))

        # same procedure as in the construction of b in the Sider ctor,
        # but with 90 minus zenith angle=altitude

        assert isinstance(horizNcp.az, astropy.coordinates.Angle)
        assert isinstance(horizNcp.alt, astropy.coordinates.Angle)

        starNcp[0] = math.sin(horizNcp.az.radian) * math.cos(horizNcp.alt.radian)
        starNcp[1] = math.cos(horizNcp.az.radian) * math.cos(horizNcp.alt.radian)
        starNcp[2] = math.sin(horizNcp.alt.radian)

        # image of targNcp while hitting M1
        m1img = trans.apply(m2tom1)

        # image of targNcp before arriving at M1
        star_off_m1 = m2tom1 + starNcp

        starimg = trans.apply(star_off_m1)

        # virtual direction of ray as seen from point after M2
        # no need to normalize this because the atan2 will do...
        # sign was wrong until 2022-11-19: we need to take the direction
        # from the on-axis star (m1img) to the off-axis start (starimg).
        starvirt = starimg - m1img

        # project in a plane orthogonal to  self.b
        cos_fang = numpy.dot(starvirt, self.box)
        sin_fang = numpy.dot(starvirt, self.boy)

        return numpy.degrees(math.atan2(sin_fang, cos_fang))


class HomTrans:
    """A single affine coordinate transformation.

    Represented internally by a 4x4 matrix as a projective.

    From https://github.com/sdss/lvmtipo/blob/main/python/lvmtipo/homtrans.py

    """

    def __init__(self, entries: numpy.ndarray):
        if isinstance(entries, numpy.ndarray):
            self.matr = entries
        else:
            self.matr = numpy.array(entries, numpy.double)

    def multiply(self, rhs: HomTrans | numpy.ndarray):
        """Multiplies by another transformation.

        Parameters
        ----------
        rhs
            The transformation to the right of the multiplication
            sign. So rhs is applied before this transformation.

        Returns
        -------
        hom_trans
         The homogeneous transformation which is the (non-communtative)
         product of self with rhs, representing the consecutive
         application of rhs, then self.

        """

        if isinstance(rhs, HomTrans):
            prod = numpy.matmul(self.matr, rhs.matr)
            return HomTrans(prod)
        elif isinstance(rhs, numpy.ndarray):
            prod = numpy.matmul(self.matr, rhs)
            return HomTrans(prod)

        raise TypeError("Invalid data types")

    def apply(self, rhs: numpy.ndarray):
        """Apply self transformation to a vector of coordinates.

        Parameters
        ----------
        rhs
            The vector. If it has only the standard 3 coordinates,
            a virtual 1 is appended before applying the transformation.

        Returns
        -------
        vector
            A vector of 3 (standard, projected) Cartesian coordinates.

        """

        if isinstance(rhs, numpy.ndarray):
            if rhs.ndim == 1:
                if rhs.shape[0] == 4:
                    prod = numpy.dot(self.matr, rhs)
                elif rhs.shape[0] == 3:
                    w = numpy.append(rhs, [1])
                    prod = numpy.dot(self.matr, w)
                else:
                    raise TypeError("Vector has invalid length.")

                prod /= prod[3]

                return numpy.array([prod[0], prod[1], prod[2]], numpy.double)

            raise TypeError("rhs not  a vector")
        raise TypeError("rhs not numpy array")


class Mirror:
    """A flat mirror.

    This represents an infintely large flat plane. The internal representation is the
    surface normal and the standard equation that the dot product of points on the
    surface by the surface normal equals the distance (of the plane to the origin of
    coordinates).

    From https://github.com/sdss/lvmtipo/blob/main/python/lvmtipo/mirror.py

    Parameters
    ----------
    normal
        The 3 Cartesian coordinates of the surface normal. It must have nonzero
        length, but does not need to be normalized to unit length.
    disttoorg
        The distance of the mirror to the origin of coordinates.
        As in usual geometry, the distance is the shortest distance of the origin
        to the infinitely extended mirror plane.

    """

    def __init__(self, normal: numpy.ndarray, disttoorg: float):
        if isinstance(normal, numpy.ndarray) and isinstance(disttoorg, (int, float)):
            self.d = float(disttoorg)
            if normal.ndim == 1 and normal.shape[0] == 3:
                vlen = numpy.linalg.norm(normal)
                normal /= vlen
                self.n = normal
            else:
                raise TypeError("invalid data types")
        else:
            raise TypeError("invalid data types")

    def to_hom_trans(self):
        """The homogeneous transformation that represents the reflection
        of rays off this mirror surface.

        """

        matr = numpy.zeros((4, 4))

        for r in range(4):
            for c in range(4):
                if r == c:
                    matr[r, c] += 1.0
                if r < 3:
                    if c < 3:
                        matr[r, c] -= 2.0 * self.n[r] * self.n[c]
                    else:
                        matr[r, c] = 2.0 * self.d * self.n[r]

        return HomTrans(matr)
