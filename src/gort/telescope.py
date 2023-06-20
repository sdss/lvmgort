#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-10
# @Filename: telescope.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from gort import config, log
from gort.gort import GortDevice, GortDeviceSet, RemoteActor
from gort.tools import get_calibrators, get_next_tile_id


if TYPE_CHECKING:
    from gort.core import ActorReply
    from gort.gort import GortClient


class Telescope(GortDevice):
    """Class representing an LVM telescope functionality."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

        self.pwi = self.actor

        kmirror_actor = kwargs.get("kmirror", None)
        self.km: RemoteActor | None = None
        self.has_kmirror = False
        if kmirror_actor:
            self.has_kmirror = True
            self.km = self.gort.add_actor(kmirror_actor)

        fibsel = "lvm.spec.fibsel"
        self.fibsel = self.gort.add_actor(fibsel) if self.name == "spec" else None

    async def update_status(self):
        """Retrieves the status of the telescope."""

        reply: ActorReply = await self.pwi.commands.status()
        self.status = reply.flatten()

        return self.status

    async def is_ready(self):
        """Checks if the telescope is ready to be moved."""

        status = await self.update_status()

        is_connected = status.get("is_connected", False)
        is_enabled = status.get("is_enabled", False)

        return is_connected and is_enabled

    async def initialise(self, home: bool | None = None):
        """Connects to the telescope and initialises the axes."""

        if not (await self.is_ready()):
            await self.pwi.commands.setConnected(True)
            await self.pwi.commands.setEnabled(True)

            if home is True or home is None:
                log.warning(f"{self.name}: homing telescope")
                await self.home()
                return

        if home is True:
            await self.home()

    async def home(self):
        """Initialises and homes the telescope."""

        await self.initialise(home=False)
        await self.pwi.commands.findHome()

        if self.km:
            await self.km.commands.moveToHome()

        # findHome does not block, so wait a reasonable amount of time.
        await asyncio.sleep(30)

    async def park(
        self,
        disable=True,
        use_pw_park=False,
        alt_az: tuple[float, float] | None = None,
        kmirror: bool = True,
    ):
        """Parks the telescope."""

        await self.initialise()

        if use_pw_park:
            await self.pwi.commands.park()
        elif alt_az is not None:
            await self.pwi.commands.gotoAltAzJ2000(*alt_az)
        else:
            coords = config["telescopes"]["named_positions"]["park"]["all"]
            await self.pwi.commands.gotoAltAzJ2000(coords["alt"], coords["az"])

        if disable:
            await self.pwi.commands.setEnabled(False)

        if kmirror and self.km:
            await self.km.commands.slewStop()
            await self.km.commands.moveAbsolute(90, "DEG")

    async def goto_coordinates(
        self,
        ra: float | None = None,
        dec: float | None = None,
        alt: float | None = None,
        az: float | None = None,
        kmirror: bool = True,
        altaz_tracking: bool = False,
    ):
        """Moves the telescope to a given RA/Dec or Alt/Az.

        Parameters
        ----------
        ra
            Right ascension coordinates to move to.
        dec
            Declination coordinates to move to.
        alt
            Altitude coordinates to move to.
        az
            Azimuth coordinates to move to.
        kmirror
            Whether to move the k-mirror into position. Only when
            the coordinates provided are RA/Dec.
        altaz_tracking
            If `True`, starts tracking after moving to alt/az coordinates.
            By defaul the PWI won't track with those coordinates.

        """

        if ra is not None and dec is not None:
            is_radec = ra is not None and dec is not None and not alt and not az
            assert is_radec, "Invalid input parameters"

            await self.initialise()
            await self.pwi.commands.gotoRaDecJ2000(ra / 15.0, dec)

        elif alt is not None and az is not None:
            is_altaz = alt is not None and az is not None and not ra and not dec
            assert is_altaz, "Invalid input parameters"

            await self.initialise()
            await self.pwi.commands.gotoAltAzJ2000(alt, az)
            if altaz_tracking:
                await self.pwi.commands.setTracking(enable=True)

        if kmirror and self.km and ra and dec:
            await self.km.commands.slewStart(ra / 15.0, dec)

    async def goto_mask_position(self, position: str | int):
        """Moves the spectrophotometric mask to the desired position."""

        if self.name != "spec" or not self.fibsel:
            raise ValueError("goto_mask_position can only be used with spec.")

        if isinstance(position, str):
            mask_positions = config["telescope"]["mask_positions"]
            if position not in mask_positions:
                raise ValueError(f"Cannot find position {position!r}.")

            motor_position = mask_positions[position]
        else:
            motor_position = position

        await self.fibsel.commands.moveAbsolute(motor_position)


class TelescopeSet(GortDeviceSet[Telescope]):
    """A representation of a set of telescopes."""

    __DEVICE_CLASS__ = Telescope

    async def initialise(self):
        """Initialise all telescopes."""

        await asyncio.gather(*[tel.initialise() for tel in self.values()])

    async def home(self):
        """Initialises and homes all telescopes."""

        await asyncio.gather(*[tel.home() for tel in self.values()])

    async def park(
        self,
        use_pw_park=False,
        alt_az: tuple[float, float] | None = None,
        disable=True,
        kmirror=True,
    ):
        """Parks the telescopes."""

        await asyncio.gather(
            *[
                tel.park(
                    disable=disable,
                    use_pw_park=use_pw_park,
                    alt_az=alt_az,
                    kmirror=kmirror,
                )
                for tel in self.values()
            ]
        )

    async def goto_coordinates_all(
        self,
        ra: float | None = None,
        dec: float | None = None,
        alt: float | None = None,
        az: float | None = None,
        kmirror: bool = True,
        altaz_tracking: bool = False,
    ):
        """Moves all the telescopes to a given RA/Dec or Alt/Az.

        Parameters
        ----------
        ra
            Right ascension coordinates to move to.
        dec
            Declination coordinates to move to.
        alt
            Altitude coordinates to move to.
        az
            Azimuth coordinates to move to.
        kmirror
            Whether to move the k-mirror into position. Only when
            the coordinates provided are RA/Dec.
        altaz_tracking
            If `True`, starts tracking after moving to alt/az coordinates.
            By defaul the PWI won't track with those coordinates.

        """

        await asyncio.gather(
            *[
                tel.goto_coordinates(
                    ra=ra,
                    dec=dec,
                    alt=alt,
                    az=az,
                    kmirror=kmirror,
                    altaz_tracking=altaz_tracking,
                )
                for tel in self.values()
            ]
        )

    async def goto_named_position(self, name: str, altaz_tracking: bool = False):
        """Sends the telescopes to a named position."""

        if name not in config["telescope"]["named_positions"]:
            raise ValueError(f"Invalid named position {name!r}.")

        position_data = config["telescope"]["named_positions"][name]

        coros = []
        for tel in self.values():
            name = tel.name
            if name in position_data:
                coords = position_data[name]
            elif "all" in position_data:
                coords = position_data["all"]
            else:
                raise ValueError(f"Cannot find position data for {name!r}.")

            if "alt" in coords and "az" in coords:
                coro = tel.goto_coordinates(
                    alt=coords["alt"],
                    az=coords["az"],
                    altaz_tracking=altaz_tracking,
                )
            elif "ra" in coords and "dec" in coords:
                coro = tel.goto_coordinates(ra=coords["ra"], dec=coords["dec"])
            else:
                raise ValueError(f"No ra/dec or alt/az coordinates found for {name!r}.")

            coros.append(coro)

        await asyncio.gather(*coros)

    async def goto_tile_id(
        self,
        tile_id: int | None = None,
        ra: float | None = None,
        dec: float | None = None,
    ):
        """Moves all the telescopes to a ``tile_id``.

        If the ``tile_id`` is not provided, the next tile is retrieved from
        the scheduler. If ``ra``/``dec`` are provided, the science telescope
        will point to those coordinates and the remaining will be grabbed from
        the scheduler.

        """

        tile_id_data: dict = {}
        if tile_id is None and (ra is None and dec is None):
            tile_id_data = await get_next_tile_id()
            calibrators = await get_calibrators(tile_id=tile_id_data["tile_id"])
        elif ra is not None and dec is not None:
            tile_id_data = {"tile_id": None, "tile_pos": (ra, dec)}
            calibrators = await get_calibrators(ra=ra, dec=dec)
        else:
            raise ValueError("Both ra and dec need to be provided.")

        tile_id = tile_id_data["tile_id"]

        sci = (tile_id_data["tile_pos"][0], tile_id_data["tile_pos"][1])
        skye, skyw = calibrators["sky_pos"]
        spec = calibrators["standard_pos"][0]

        log.info(f"Going to tile_id={tile_id}.")
        log.debug(f"Science: {sci}")
        log.debug(f"Spec: {spec}")
        log.debug(f"SkyE: {skye}")
        log.debug(f"SkyW: {skyw}")

        await self.goto(sci=sci, spec=spec, skye=skye, skyw=skyw)

        tile_id_data.update(calibrators)
        return tile_id_data

    async def goto(
        self,
        sci: tuple[float, float] | None = None,
        spec: tuple[float, float] | None = None,
        skye: tuple[float, float] | None = None,
        skyw: tuple[float, float] | None = None,
    ):
        """Sends each telescope to a different position."""

        jobs = []

        if sci is not None:
            jobs.append(self["sci"].goto_coordinates(ra=sci[0], dec=sci[1]))

        if spec is not None:
            jobs.append(self["spec"].goto_coordinates(ra=spec[0], dec=spec[1]))

        if skye is not None:
            jobs.append(self["skye"].goto_coordinates(ra=skye[0], dec=skye[1]))

        if skyw is not None:
            jobs.append(self["skyw"].goto_coordinates(ra=skyw[0], dec=skyw[1]))

        if len(jobs) == 0:
            return

        await asyncio.gather(*jobs)
