#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-25
# @Filename: ag.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from gort.gort import GortDevice, GortDeviceSet


__all__ = ["AG", "AGSet"]


class AG(GortDevice):
    """Class representing an AG camera."""

    async def status(self):
        """Returns the status of the AG."""

        return await self.actor.commands.status()

    async def reconnect(self):
        """Reconnect the AG cameras."""

        self.write_to_log("Reconnecting AG cameras.")

        return await self.actor.commands.reconnect()

    async def expose(
        self,
        exposure_time: float = 5.0,
        flavour: str = "object",
        **kwargs,
    ):
        """Exposes the cameras.

        Parameters
        ----------
        exposure_time
            The amount of time, in seconds, to expose the cameras.
        flavour
            The type of image to take, one of ``'bias'``, ``'dark'``, ``'flat'``,
            or ``'object'``.
        kwargs
            Any parameters to send to the ``lvmcam expose`` command.

        """

        if flavour == "bias":
            kwargs["bias"] = True
        elif flavour == "flat":
            kwargs["flat"] = True
        elif flavour == "dark":
            kwargs["dark"] = True
        else:
            kwargs["object"] = True

        return await self.actor.commands.expose(
            exptime=exposure_time,
            **kwargs,
        )


class AGSet(GortDeviceSet[AG]):
    """A set of auto-guiders."""

    __DEVICE_CLASS__ = AG
    __DEPLOYMENTS__ = ["lvmagcam"]

    async def reconnect(self):
        """Reconnects all the AG cameras.

        Returns
        -------
        cameras
            A list of cameras available after reconnecting.

        """

        await asyncio.gather(*[ag.reconnect() for ag in self.values()])

        return await self.list_alive_cameras()

    async def list_alive_cameras(self):
        """Returns a list of cameras found alive and well.

        Currently when a camera has disconnected, the status command still reports
        it as "online", but it doesn't report its temperature or other parameters.

        """

        all_status = await asyncio.gather(*[ag.status() for ag in self.values()])

        cameras = []
        for reply in all_status:
            actor = reply.actor.name
            telescope = actor.split(".")[1]
            messages = reply.flatten()
            if "east" in messages and "temperature" in messages["east"]:
                cameras.append(f"{telescope}-e")
            if "west" in messages and "temperature" in messages["west"]:
                cameras.append(f"{telescope}-w")

        return cameras

    async def expose(
        self,
        exposure_time: float = 5.0,
        flavour: str = "object",
        **kwargs,
    ):
        """Exposes the cameras.

        Parameters
        ----------
        exposure_time
            The amount of time, in seconds, to expose the cameras.
        flavour
            The type of image to take, one of ``'bias'``, ``'dark'``, ``'flat'``,
            or ``'object'``.
        kwargs
            Any parameters to send to the ``lvmcam expose`` command.

        """

        return await asyncio.gather(
            *[
                ag.expose(
                    exposure_time=exposure_time,
                    flavour=flavour,
                    **kwargs,
                )
                for ag in self.values()
            ]
        )
