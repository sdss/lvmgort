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

from trurl import config


if TYPE_CHECKING:
    from trurl.core import ActorReply, Trurl


class Telescope:
    """Class representing an LVM telescope functionality.

    Parameters
    ----------
    trurl
        The `.Trurl` instance used to communicate with the actor system.
    name
        The name of the telescope.
    pwi_actor
        The PlaneWave mount control actor. If `None`, uses ``lvm.{name}.pwi``.
    agcam_actor
        The autoguide camera actor. If `None`, uses ``lvm.{name}.agcam``.
    agp_actor
        The autoguide actor. If `None`, uses ``lvm.{name}.agp``.

    """

    def __init__(
        self,
        trurl: Trurl,
        name: str,
        pwi_actor: str | None = None,
        agcam_actor: str | None = None,
        agp_actor: str | None = None,
    ):
        self.name = name
        self.trurl = trurl

        self._pwi_actor_name = pwi_actor or f"lvm.{name}.pwi"
        self._km_actor_name = pwi_actor or f"lvm.{name}.km"
        self._agcam_actor_name = agcam_actor or f"lvm.{name}.agcam"
        self._agp_actor_name = agp_actor or f"lvm.{name}.agp"

        if self.name != "spec":
            self._km_actor_name = f"lvm.{name}.km"
        else:
            self._km_actor_name = None

        self.status = {}

    async def prepare(self):
        """Prepares the telescope class for asynchronous access."""

        self.pwi = await self.trurl.add_actor(self._pwi_actor_name)
        # await self.trurl.add_actor(self.agcam_actor)
        # await self.trurl.add_actor(self.agp_actor)

        if self._km_actor_name:
            await self.trurl.add_actor(self._km_actor_name)
            self.km = self.trurl.actors[self._km_actor_name]
        else:
            self.km = None

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

    async def initialise(self):
        """Connects to the telescope and initialises the axes."""

        if not (await self.is_ready()):
            await self.pwi.commands.setConnected(True)
            await self.pwi.commands.setEnabled(True)

    async def home(self):
        """Initialises and homes the telescope."""

        await self.initialise()
        await self.pwi.commands.findHome()

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
    ):
        """Moves the telescope to a given RA/Dec or Alt/Az."""

        if ra is not None or dec is not None:
            is_radec = ra is not None and dec is not None and not alt and not az
            assert is_radec, "Invalid input parameters"

            await self.initialise()
            await self.pwi.commands.gotoRaDecJ2000(ra, dec)

        if alt is not None or az is not None:
            is_altaz = alt is not None and az is not None and not ra and not dec
            assert is_altaz, "Invalid input parameters"

            await self.initialise()
            await self.pwi.commands.gotoAltAzJ2000(alt, az)

        if kmirror and self.km and ra and dec:
            await self.km.commands.slewStart(ra, dec)


class TelescopeSet:
    """A representation of a set of telescopes."""

    def __init__(self, trurl: Trurl, names: list):
        self.names = names

        for name in names:
            setattr(self, name, Telescope(trurl, name))

    def __getitem__(self, key: str) -> Telescope:
        return getattr(self, key)

    async def prepare(self):
        """Prepares the set of telescopes for asynchronous access."""

        await asyncio.gather(*[self[tel].prepare() for tel in self.names])

    async def initialise(self):
        """Initialise all telescopes."""

        await asyncio.gather(*[self[tel].initialise() for tel in self.names])

    async def home(self):
        """Initialises and homes all telescopes."""

        await asyncio.gather(*[self[tel].home() for tel in self.names])

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
                self[tel].park(
                    disable=disable,
                    use_pw_park=use_pw_park,
                    alt_az=alt_az,
                    kmirror=kmirror,
                )
                for tel in self.names
            ]
        )

    async def goto_coordinates(
        self,
        ra: float | None = None,
        dec: float | None = None,
        alt: float | None = None,
        az: float | None = None,
        kmirror: bool = True,
    ):
        """Moves all the telescopes to a given RA/Dec or Alt/Az."""

        await asyncio.gather(
            *[
                self[tel].goto_coordinates(
                    ra=ra,
                    dec=dec,
                    alt=alt,
                    az=az,
                    kmirror=kmirror,
                )
                for tel in self.names
            ]
        )

    async def goto_named_position(self, name: str):
        """Sends the telescopes to a named position."""

        if name not in config["telescopes"]["named_positions"]:
            raise ValueError(f"Invalid named position {name!r}.")

        position_data = config["telescopes"]["named_positions"][name]

        coros = []
        for tel in self.names:
            if tel in position_data:
                coords = position_data[tel]
            elif "all" in position_data:
                coords = position_data["all"]
            else:
                raise ValueError(f"Cannot find position data for {name:!r}.")

            if "alt" in coords and "az" in coords:
                coro = self[tel].goto_coordinates(alt=coords["alt"], az=coords["az"])
            elif "ra" in coords and "dec" in coords:
                coro = self[tel].goto_coordinates(ra=coords["ra"], dec=coords["dec"])
            else:
                raise ValueError(f"No ra/dec or alt/az coordinates found for {name!r}.")

            coros.append(coro)

        await asyncio.gather(*coros)
