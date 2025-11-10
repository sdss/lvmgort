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
from gort.exceptions import GortDeviceError
from gort.gort import Gort
from gort.tools import ping_host, run_lvmapi_task


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

        offline_cameras = self.gort.config.get("ags", {}).get("offline_cameras", [])
        self.offline_cameras = [cam for cam in offline_cameras if cam.startswith(name)]

    @property
    def n_cameras(self):
        """The number of AG cameras for this telescope."""

        all_cams = len([1 for ip in self.ips.values() if ip is not None])
        return all_cams - len(self.offline_cameras)

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

        actor_reply = await self.actor.commands.reconnect()
        for reply in actor_reply.replies:
            if "error" in reply:
                error = reply["error"]
                if "arv-device-error-quark" in error:
                    raise GortDeviceError(
                        f"One or more {self.name} cameras failed to reconnect. "
                        f"The cameras may be in locked mode. Error: {error}"
                    )
                else:
                    self.write_to_log(
                        f"Error reconnecting {self.name} cameras: {error}",
                        level="error",
                    )

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

    async def check_camera(self, ping: bool = True, status: bool = True):
        """Checks that the AG cameras are responding.

        Parameters
        ----------
        ping
            Whether to ping the cameras.
        status
            Whether to check the camera status.

        """

        if not ping and not status:
            raise ValueError("At least one of ping or status must be True.")

        failed: set[str] = set()

        if ping:
            for side, ip in self.ips.items():
                name = f"{self.name}-{side}"

                if ip is None:
                    continue

                if not await ping_host(ip):
                    self.write_to_log(f"AG {name} did not ping.", "warning")
                    failed.add(name)
                else:
                    self.write_to_log(f"AG {name} pinged back.", "debug")

        if status:
            try:
                await self.status()
            except Exception:
                failed.update(self.ips.keys())

        if len(failed) == 0:
            return True

        raise GortDeviceError(
            f"The following AG cameras are not responding: {', '.join(failed)}."
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

        replies = await asyncio.gather(
            *[ag.reconnect() for ag in self.values()],
            return_exceptions=True,
        )

        for reply in replies:
            if isinstance(reply, BaseException):
                raise

        return await self.list_alive_cameras()

    async def are_idle(self):
        """Returns :obj:`True` if all the cameras are idle."""

        return all(await asyncio.gather(*[ag.is_idle() for ag in self.values()]))

    async def check_cameras(self, allow_power_cycle: bool = True):
        """Checks that all cameras are responding.

        Parameters
        ----------
        allow_power_cycle
            Whether to allow power cycling the cameras if they are not responding.

        """

        try:
            await asyncio.gather(*[ag.check_camera() for ag in self.values()])
        except GortDeviceError:
            if not allow_power_cycle:
                raise GortDeviceError("One or more AG cameras are not responding.")

            self.write_to_log(
                "One or more AG cameras are not responding. Power cycling...",
                "warning",
            )
            await self.power_cycle()

            await self.check_cameras(allow_power_cycle=False)

        return True

    async def list_alive_cameras(self):
        """Returns a list of cameras found alive and well.

        Currently when a camera has disconnected, the status command still reports
        it as "online", but it doesn't report its temperature or other parameters.

        """

        all_status = await asyncio.gather(
            *[ag.status() for ag in self.values()],
            return_exceptions=True,
        )

        replies = []
        for status in all_status:
            if isinstance(status, BaseException):
                continue

            for reply in status.replies:
                try:
                    replies.append({"actor": status.actor.name, **reply["status"]})
                except Exception:
                    pass

        cameras: list[str] = []
        for reply in replies:
            actor = reply["actor"]
            telescope = actor.split(".")[1]
            camera = reply.get("camera", None)
            state = reply.get("camera_state", None)
            if camera and state:
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

        await run_lvmapi_task(
            "/macros/power_cycle_ag_cameras",
            params={"reconnect": False},
        )

        # Restart the actors
        await self.restart()
        await asyncio.sleep(15)

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
