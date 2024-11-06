#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-08
# @Filename: tile.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import warnings

from typing import Sequence, cast

import polars
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time
from httpx import RequestError

from gort import config
from gort.exceptions import (
    ErrorCode,
    GortError,
    GortNotImplemented,
    GortWarning,
    TileError,
)
from gort.tools import (
    get_by_source_id,
    get_calibrators_sync,
    get_db_connection,
    get_next_tile_id_sync,
    set_tile_status,
)
from gort.transforms import fibre_to_master_frame, offset_to_master_frame_pixel


__all__ = [
    "Coordinates",
    "QuerableCoordinates",
    "ScienceCoordinates",
    "SkyCoordinates",
    "StandardCoordinates",
    "Tile",
]


CoordTuple = tuple[float, float]


class Coordinates:
    """Basic coordinates class.

    Parameters
    ----------
    ra
        The RA coordinate, in degrees. FK5 frame at the epoch of observation.
    dec
        The Dec coordinate, in degrees.
    pa
        Position angle of the IFU. Defaults to PA=0.
    centre_on_fibre
        The name of the fibre on which to centre the target, with the format
        ``<ifulabel>-<finufu>``. By default, acquires the target on the central
        fibre of the science IFU.

    """

    def __init__(
        self,
        ra: float,
        dec: float,
        pa: float | None = None,
        centre_on_fibre: str | None = None,
    ):
        self.ra = ra
        self.dec = dec
        self.pa = pa % 360 if pa is not None else 0.0

        self.skycoord = SkyCoord(ra=ra, dec=dec, unit="deg", frame="fk5")

        self.centre_on_fibre = centre_on_fibre

        # The MF pixel on which to guide/centre the target.
        self._mf_pixel = self.set_mf_pixel(centre_on_fibre)

    def __repr__(self):
        return (
            f"<{self.__class__.__name__} "
            f"(ra={self.ra:.6f}, dec={self.dec:.6f}, pa={self.pa:.3f})>"
        )

    def __str__(self):
        return f"{self.ra:.6f}, {self.dec:.6f}, {self.pa:.3f}"

    def calculate_altitude(self, time: Time | None = None):
        """Returns the current altitude of the target."""

        if time is None:
            time = Time.now()

        location = EarthLocation.from_geodetic(**config["site"])

        sc = self.skycoord.copy()
        sc.obstime = time
        sc.location = location
        altaz = sc.transform_to("altaz")

        return altaz.alt.deg

    def is_observable(self):
        """Determines whether a target is observable."""

        return self.calculate_altitude() > 30

    def set_mf_pixel(self, fibre_name: str | None = None, xz: CoordTuple | None = None):
        """Calculates and sets the master frame pixel on which to centre the target.

        If neither ``fibre_name`` or ``xz`` are passed, resets to centring
        the target on the central fibre of the IFU.

        Parameters
        ----------
        fibre_name
            The fibre to which to centre the target, with the format
            ``<ifulabel>-<finifu>``.
        xz
            The coordinates, in master frame pixels, on which to centre
            the target.

        Returns
        -------
        pixel
            A tuple with the x and z coordinates of the pixel in the master frame,
            or :obj:`None` if resetting to the central fibre.

        """

        if fibre_name is not None:
            xmf, zmf = fibre_to_master_frame(fibre_name)
        elif xz is not None:
            xmf, zmf = xz
        else:
            self._mf_pixel = None
            return None

        self._mf_pixel = (xmf, zmf)

        return (xmf, zmf)


class QuerableCoordinates(Coordinates):
    """A class of coordinates that can be retrieved from the database."""

    __db_table__: str = ""
    targets: SkyCoord | None = None

    @classmethod
    def from_science_coordinates(
        cls,
        sci_coords: ScienceCoordinates,
        exclude_coordinates: Sequence[CoordTuple] = [],
        exclude_invisible: bool = True,
    ):
        """Retrieves a valid and observable position from the database.

        Parameters
        ----------
        sci_coords
            The science coordinates. The position selected will be the
            closest to these coordinates.
        exclude_coordinates
            A list of RA/Dec coordinates to exclude. No region closer
            than one degree to these coordinates will be selected.
        exclude_invisible
            Exclude targets that are too low.

        """

        connection = get_db_connection()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            targets = polars.read_database(
                f"SELECT ra,dec from {cls.__db_table__};",
                connection,
            )

        # Cache query.
        if cls.targets is None:
            cls.targets = SkyCoord(
                ra=targets["ra"].to_list(),
                dec=targets["dec"].to_list(),
                unit="deg",
            )

        assert cls.targets is not None
        skycoords = cls.targets.copy()

        # Exclude regions too close to the exlcuded ones.
        for ex_coords in exclude_coordinates:
            ex_skycoords = SkyCoord(ra=ex_coords[0], dec=ex_coords[1], unit="deg")
            skycoords = skycoords[skycoords.separation(ex_skycoords).deg > 1]

        # Exclude targets that are too low.
        if exclude_invisible:
            skycoords.location = EarthLocation.from_geodetic(**config["site"])

            skycoords.obstime = Time.now()
            altaz_skycoords = skycoords.transform_to("altaz")
            skycoords = skycoords[altaz_skycoords.alt.deg > 30]

        if len(skycoords) == 0:
            raise TileError("No sky coordinates found.")

        seps = skycoords.separation(sci_coords.skycoord)
        skycoord_min = skycoords[seps.argmin()]

        return cls(skycoord_min.ra.deg, skycoord_min.dec.deg)

    def verify_and_replace(self, exclude_coordinates: Sequence[CoordTuple] = []):
        """Verifies that the coordinates are visible and if not, replaces them.

        Parameters
        ----------
        exclude_coordinates
            A list of RA/Dec coordinates to exclude. No region closer
            than one degree to these coordinates will be selected.

        """

        if not self.is_observable():
            # Use current coordinates as proxy for the science telescope.
            sci_coords = ScienceCoordinates(self.ra, self.dec)
            valid_skycoords = self.from_science_coordinates(
                sci_coords,
                exclude_coordinates=exclude_coordinates,
            )
            super().__init__(valid_skycoords.ra, valid_skycoords.dec)


class ScienceCoordinates(Coordinates):
    """A science position.

    Parameters
    ----------
    ra
        The RA coordinate, in degrees. FK5 frame at the epoch of observation.
    dec
        The Dec coordinate, in degrees.
    pa
        Position angle of the IFU. Defaults to PA=0.
    centre_on_fibre
        The name of the fibre on which to centre the target, with the format
        ``<ifulabel>-<finufu>``. By default, acquires the target on the central
        fibre of the science IFU.

    """

    dither_position: int = 0


class SkyCoordinates(QuerableCoordinates):
    """A sky position.

    In addition to the `.QuerableCoordinates` arguments the class accepts
    a ``name`` identifier.

    """

    __db_table__ = "lvmopsdb.sky"

    def __init__(self, *args, name: str | None = None, **kwargs):
        self.name = name
        self.pk: int | None = None

        super().__init__(*args, **kwargs)


class StandardCoordinates(QuerableCoordinates):
    """A standard position.

    In addition to the `.QuerableCoordinates` arguments the class accepts
    a ``source_id`` Gaia identifier.

    """

    __db_table__ = "lvmopsdb.standard"

    def __init__(
        self,
        ra: float | None = None,
        dec: float | None = None,
        source_id: int | None = None,
        **kwargs,
    ):
        self.source_id = source_id
        self.pk: int | None = None

        if ra is None or dec is None:
            if source_id is None:
                raise TileError("Must pass either ra/dec or source_id.")
            else:
                if (data := get_by_source_id(int(source_id))) is None:
                    raise TileError(f"Cannot find Gaia data for source_id={source_id}.")

                ra = data["ra"]
                dec = data["dec"]

        assert ra is not None and dec is not None

        super().__init__(ra, dec, **kwargs)


SpecCoordsType = Sequence[StandardCoordinates | CoordTuple | int | dict] | None
SkyCoordsType = dict[str, SkyCoordinates] | dict[str, CoordTuple] | None


class Tile(dict[str, Coordinates | Sequence[Coordinates] | None]):
    """A representation of a science pointing with associated calibrators.

    This class is most usually initialised from a classmethod like
    :obj:`.from_scheduler`.

    Parameters
    ----------
    sci_coords
        The science telescope pointing.
    sky_coords
        A dictionary of ``skye`` and ``skyw`` coordinates.
    spec_coords
        A list of coordinates to observe with the spectrophotometric telescope.
    dither_position
        The dither position(s) to obseve.
    object
        The name of the object.
    allow_replacement
        If :obj:`True`, allows the replacement of empty, invalid or low altitude
        sky and standard targets.

    """

    def __init__(
        self,
        sci_coords: ScienceCoordinates,
        sky_coords: SkyCoordsType = None,
        spec_coords: SpecCoordsType = None,
        dither_positions: int | Sequence[int] = 0,
        object: str | None = None,
        allow_replacement: bool = True,
    ):
        self.allow_replacement = allow_replacement

        self.tile_id: int | None = None
        self.dither_positions = (
            dither_positions
            if isinstance(dither_positions, Sequence)
            else [dither_positions]
        )

        self.object = object or (f"Tile {self.tile_id}" if self.tile_id else None)

        dict.__init__(self, {})

        self.set_sci_coords(sci_coords)
        if isinstance(dither_positions, int):
            self.set_dither_position(dither_positions)
        else:
            self.set_dither_position(dither_positions[0])

        self.set_sky_coords(sky_coords, allow_replacement=allow_replacement)
        self.set_spec_coords(spec_coords, reject_invisible=allow_replacement)

    def __repr__(self):
        return (
            "<Tile "
            f"(tile_id={self.tile_id}, "
            f"science ra={self.sci_coords.ra:.6f}, "
            f"dec={self.sci_coords.dec:.6f}, "
            f"pa={self.sci_coords.pa:.3f}; "
            f"n_skies={len(self.sky_coords)}; "
            f"n_standards={len(self.spec_coords)}; "
            f"dither_positions={self.dither_positions!r})>"
        )

    def set_dither_position(self, dither: int):
        """Sets the full frame pixel for the science IFU to the dither position."""

        raoff: float
        decoff: float
        raoff, decoff = config["guiders"]["devices"]["sci"]["dither_offsets"][dither]

        xx, zz = offset_to_master_frame_pixel(ra=raoff, dec=decoff)
        self.sci_coords.set_mf_pixel(xz=(xx, zz))

        self.sci_coords.dither_position = dither

    @property
    def sci_coords(self):
        """Returns the science coordinates."""

        return cast(ScienceCoordinates, self["sci"])

    @sci_coords.setter
    def sci_coords(self, new_coords: ScienceCoordinates):
        """Sets the science coordinates."""

        if isinstance(new_coords, (list, tuple)):
            new_coords = ScienceCoordinates(*new_coords)

        self["sci"] = new_coords

    @property
    def sky_coords(self) -> dict[str, SkyCoordinates]:
        """Returns the sky coordinates."""

        skyw = cast(SkyCoordinates, self["skyw"])
        skye = cast(SkyCoordinates, self["skye"])

        return {"skye": skye, "skyw": skyw}

    @sky_coords.setter
    def sky_coords(self, new_coords: dict[str, SkyCoordinates]):
        """Returns the sky coordinates."""

        for tel in ["skye", "skyw"]:
            sky = new_coords.get(tel, None)
            if isinstance(sky, (tuple, list)):
                sky = SkyCoordinates(*sky)
            self[tel] = sky

    @property
    def spec_coords(self):
        """Returns the Spec coordinates."""

        return cast(Sequence[StandardCoordinates], self["spec"])

    @spec_coords.setter
    def spec_coords(self, new_coords: Sequence[StandardCoordinates]):
        """Sets the SkyW coordinates."""

        parsed_coords: Sequence[Coordinates] = []
        for coords in new_coords:
            if isinstance(coords, (list, tuple)):
                parsed_coords.append(StandardCoordinates(*coords))
            else:
                parsed_coords.append(coords)

        self["spec"] = parsed_coords

    @classmethod
    def from_coordinates(
        cls,
        ra: float,
        dec: float,
        pa: float = 0.0,
        sky_coords: SkyCoordsType = None,
        spec_coords: SpecCoordsType | None = None,
        **kwargs,
    ):
        """Creates an instance from coordinates, allowing autocompletion.

        Parameters
        ----------
        ra,dec
            The science telescope pointing.
        pa
            Position angle of the science IFU. Defaults to PA=0.
        sky_coords
            A dictionary of ``skye`` and ``skyw`` coordinates. If `None`,
            autocompleted from the closest available regions.
        spec_coords
            A list of coordinates to observe with the spectrophotometric telescope.
            If :obj:`None`, selects the 12 closest standard stars.
        kwargs
            Arguments to be passed to the initialiser.

        """

        sci_coords = ScienceCoordinates(ra, dec, pa=pa)

        calibrators: dict | None = None
        if sky_coords is None or spec_coords is None:
            calibrators = get_calibrators_sync(ra=ra, dec=dec)

        if sky_coords is None:
            assert calibrators is not None
            sky_coords = {}
            sky_coords["skye"] = SkyCoordinates(*calibrators["sky_pos"][0])
            sky_coords["skyw"] = SkyCoordinates(*calibrators["sky_pos"][1])

            sky_coords["skye"].pk = calibrators["sky_pks"][0]
            sky_coords["skyw"].pk = calibrators["sky_pks"][1]

        if spec_coords is None:
            assert calibrators is not None
            spec_coords = []
            for ii in range(12):
                coords = StandardCoordinates(
                    *calibrators["standard_pos"][ii],
                    source_id=calibrators["standard_ids"][ii],
                )
                coords.pk = calibrators["standard_pks"][ii]
                spec_coords.append(coords)

        return cls(
            sci_coords,
            spec_coords=spec_coords,
            sky_coords=sky_coords,
            allow_replacement=False,
            **kwargs,
        )

    @classmethod
    def from_scheduler(
        cls,
        tile_id: int | None = None,
        ra: float | None = None,
        dec: float | None = None,
        pa: float = 0.0,
        **kwargs,
    ):
        """Creates a new instance of :obj:`.Tile` with data from the scheduler.

        Parameters
        ----------
        tile_id
            The ``tile_id`` for which to create a new :obj:`.Tile`. If :obj:`None`,
            and ``ra`` and ``dec`` are also :obj:`None`, the best ``tile_id``
            selected by the scheduler will be used.
        ra
            Right ascension coordinates of the science telescope pointing.
            Calibrators will be selected from the scheduler.
        dec
            Declination coordinates of the science telescope pointing.
        pa
            Position angle of the science IFU. Defaults to PA=0.
        kwargs
            Arguments to be passed to the initialiser.

        """

        if tile_id is None and ra is None and dec is None:
            try:
                tile_id_data = get_next_tile_id_sync()
            except RequestError:
                raise TileError(
                    "Cannot retrieve tile_id from scheduler.",
                    error_code=ErrorCode.SCHEDULER_CANNOT_FIND_TILE,
                )

            tile_id = tile_id_data["tile_id"]
            sci_pos = tile_id_data["tile_pos"]
            dither_pos = tile_id_data["dither_pos"]

            if tile_id is None or tile_id < 0:
                raise GortError(
                    "The scheduler could not find a valid tile to observe.",
                    error_code=ErrorCode.SCHEDULER_CANNOT_FIND_TILE,
                )

        elif tile_id is not None:
            raise GortNotImplemented("Initialising from a tile_id is not supported.")

        elif not tile_id and (ra is not None and dec is not None):
            tile_id = None
            sci_pos = (ra, dec, pa)
            dither_pos = 0

        else:
            raise TileError("Invalid inputs.")

        sci_coords = ScienceCoordinates(*sci_pos, centre_on_fibre=None)

        if tile_id:
            calibrator_data = get_calibrators_sync(tile_id=tile_id)
        else:
            calibrator_data = get_calibrators_sync(ra=sci_pos[0], dec=sci_pos[1])

        sky_coords = {
            "skye": SkyCoordinates(
                *calibrator_data["sky_pos"][0],
                name=calibrator_data["sky_names"][0],
            ),
            "skyw": SkyCoordinates(
                *calibrator_data["sky_pos"][1],
                name=calibrator_data["sky_names"][1],
            ),
        }

        sky_coords["skye"].pk = calibrator_data["sky_pks"][0]
        sky_coords["skyw"].pk = calibrator_data["sky_pks"][1]

        spec_coords = []
        for ii in range(len(calibrator_data["standard_pos"])):
            std_coords = StandardCoordinates(
                *calibrator_data["standard_pos"][ii],
                source_id=calibrator_data["standard_ids"][ii],
            )
            std_coords.pk = calibrator_data["standard_pks"][ii]
            spec_coords.append(std_coords)

        new_obj = cls(
            sci_coords,
            sky_coords=sky_coords,
            spec_coords=spec_coords,
            dither_positions=dither_pos,
            **kwargs,
        )
        new_obj.tile_id = tile_id

        return new_obj

    def set_sci_coords(
        self,
        sci_coords: ScienceCoordinates | CoordTuple,
    ) -> ScienceCoordinates:
        """Sets the science telescope coordinates.

        Parameters
        ----------
        sci_coords
            A :obj:`.ScienceCoordinates` object or a tuple with RA/Dec
            coordinates for the science telescope.

        """

        if isinstance(sci_coords, ScienceCoordinates):
            self.sci_coords = sci_coords
        else:
            self.sci_coords = ScienceCoordinates(*sci_coords)

        return self.sci_coords

    def set_sky_coords(
        self,
        sky_coords: SkyCoordsType = None,
        allow_replacement: bool = True,
    ) -> dict[str, SkyCoordinates]:
        """Sets the sky telescopes coordinates.

        Parameters
        ----------
        sky_coords
            A dictionary of ``skye`` and ``skyw`` coordinates. Each value must
            be a :obj:`.SkyCoordinates` object or a tuple of RA/Dec coordinates.
        allow_replacement
            If :obj:`True`, allows the replacement of empty, invalid or low
            altitude targets.

        """

        if sky_coords is None:
            sky_coords = {}

        valid_sky_coords: dict[str, SkyCoordinates] = {}
        assigned_coordinates: Sequence[CoordTuple] = []

        for telescope in ["skye", "skyw"]:
            tel_coords = sky_coords.get(telescope, None)

            replace: bool = False
            if tel_coords is None:
                replace = True
            elif isinstance(tel_coords, SkyCoordinates):
                tel_coords = tel_coords
            else:
                tel_coords = SkyCoordinates(*tel_coords)

            if allow_replacement is False:
                if tel_coords is not None:
                    valid_sky_coords[telescope] = tel_coords
                continue

            # If both coordinates are assigned, check that they are not identical.
            if (
                tel_coords is not None
                and telescope == "skyw"
                and "skye" in valid_sky_coords
            ):
                if (
                    tel_coords.ra == valid_sky_coords["skye"].ra
                    and tel_coords.dec == valid_sky_coords["skye"].dec
                ):
                    tel_coords = None
                    replace = True

            if replace:
                try:
                    tel_coords = SkyCoordinates.from_science_coordinates(
                        self.sci_coords,
                        exclude_coordinates=assigned_coordinates,
                    )
                except Exception as err:
                    warnings.warn(
                        f"Failed getting sky coordinates for {telescope}: {err}",
                        GortWarning,
                    )
                    continue

            try:
                assert tel_coords is not None
                tel_coords.verify_and_replace(
                    exclude_coordinates=assigned_coordinates,
                )
            except Exception as err:
                warnings.warn(
                    f"Failed verifying sky coordinates for {telescope}: {err}",
                    GortWarning,
                )
                continue

            assigned_coordinates.append((tel_coords.ra, tel_coords.dec))
            valid_sky_coords[telescope] = tel_coords

        self.sky_coords = valid_sky_coords

        return self.sky_coords

    def set_spec_coords(
        self,
        spec_coords: SpecCoordsType = None,
        reject_invisible: bool = True,
    ) -> Sequence[StandardCoordinates]:
        """Sets the spec telescope coordinates.

        Parameters
        ----------
        spec_coords
            A list of coordinates to observe with the spectrophotometric telescope.
        reject_invisible
            Skip targets that are not visible now.

        """

        valid_spec_coords = []

        if spec_coords is None:
            pass
        else:
            for coords in spec_coords:
                if isinstance(coords, Coordinates):
                    pass
                elif isinstance(coords, (list, tuple)):
                    coords = StandardCoordinates(*coords)
                elif isinstance(coords, int):
                    coords = StandardCoordinates(source_id=coords)
                elif isinstance(coords, dict):
                    coords = StandardCoordinates(**coords)
                else:
                    raise TypeError(f"Invalid spec coordinate {coords!r}.")

                if reject_invisible and not coords.is_observable():
                    continue

                valid_spec_coords.append(coords)

        self.spec_coords = valid_spec_coords

        return self.spec_coords

    async def enable(self):
        """Enables the tile for observation."""

        if self.tile_id is None:
            raise TileError("Cannot enable tile without a tile_id.")

        await set_tile_status(self.tile_id, enabled=True)

    async def disable(self):
        """Disables the tile for observation."""

        if self.tile_id is None:
            raise TileError("Cannot disable tile without a tile_id.")

        await set_tile_status(self.tile_id, enabled=False)
