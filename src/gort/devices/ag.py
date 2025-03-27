#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-25
# @Filename: ag.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from gort.devices.core import GortDevice, GortDeviceSet
from gort.gort import Gort
from gort.tools import run_lvmapi_task


__all__ = ["AG", "AGSet"]


class AG(GortDevice):
    """Class representing an AG camera."""

    def __init__(self, gort: Gort, name: str, actor: str, **device_data):
        super().__init__(gort, name, actor)

        self.telescope = name
        self.ips = {
            "east": device_data["ips"]["east"]
            if "ips" in device_data and "east" in device_data["ips"]
            else None,
            "west": device_data["ips"]["west"]
            if "ips" in device_data and "west" in device_data["ips"]
            else None,
        }

    @property
    def n_cameras(self):
        """The number of AG cameras for this telescope."""

        return len([1 for ip in self.ips.values() if ip is not None])

    async def status(self):
        """Returns the status of the AG."""

        return await self.actor.commands.status()

    async def is_idle(self):
        """Returns :obj:`True` if all the cameras are idle."""

        status = await self.status()
        for reply in status.replies:
            if reply["status"]["camera_state"] != "idle":
                return False

        return True

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

    @property
    def n_cameras(self):
        """The number of AG cameras in the set."""

        return sum([ag.n_cameras for ag in self.values()])

    async def reconnect(self):
        """Reconnects all the AG cameras.

        Returns
        -------
        cameras
            A list of cameras available after reconnecting.

        """

        await asyncio.gather(*[ag.reconnect() for ag in self.values()])

        return await self.list_alive_cameras()

    async def are_idle(self):
        """Returns :obj:`True` if all the cameras are idle."""

        return all(await asyncio.gather(*[ag.is_idle() for ag in self.values()]))

    async def list_alive_cameras(self):
        """Returns a list of cameras found alive and well.

        Currently when a camera has disconnected, the status command still reports
        it as "online", but it doesn't report its temperature or other parameters.

        """

        all_status = await asyncio.gather(*[ag.status() for ag in self.values()])

        replies = []
        for status in all_status:
            for reply in status.replies:
                try:
                    replies.append({"actor": status.actor.name, **reply["status"]})
                except Exception:
                    pass

        cameras = []
        for reply in replies:
            actor = reply["actor"]
            telescope = actor.split(".")[1]
            camera = reply.get("camera", None)
            if camera:
                cameras.append(f"{telescope}-{camera}")

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

    async def power_cycle(self):
        """Power cycles all the cameras."""

        await run_lvmapi_task("/macros/power_cycle_ag_cameras")
        await asyncio.sleep(30)

        for retry in range(2):
            try:
                alive_cameras = await self.list_alive_cameras()
            except Exception:
                alive_cameras = []

            if len(alive_cameras) != self.n_cameras:
                if retry == 1:
                    raise RuntimeError("Not all cameras are responding.")

                self.write_to_log(
                    "Not all cameras are responding. Waiting 30 seconds and retrying.",
                    "warning",
                )
                await asyncio.sleep(30)
                await self.reconnect()

        return True
