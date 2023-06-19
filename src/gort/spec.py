#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-15
# @Filename: spec.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from typing import TYPE_CHECKING

from gort import config, log
from gort.exceptions import GortError
from gort.gort import GortDevice, GortDeviceSet
from gort.tools import tqdm_timer


if TYPE_CHECKING:
    from gort.core import ActorReply
    from gort.gort import GortClient


class Spectrograph(GortDevice):
    """Class representing an LVM spectrograph functionality."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

        self.status = {}

    async def update_status(self):
        """Retrieves the status of the telescope."""

        reply: ActorReply = await self.actor.commands.status()
        self.status = reply.flatten()

        return self.status

    async def expose(self, **kwargs):
        """Exposes the spectrograph."""

        await self.actor.commands.expose(**kwargs)


class SpectrographSet(GortDeviceSet[Spectrograph]):
    """A set of LVM spectrographs."""

    __DEVICE_CLASS__ = Spectrograph

    def get_seqno(self):
        """Returns the next exposure sequence number."""

        next_exposure_number_path = config["specs"]["nextExposureNumber"]
        with open(next_exposure_number_path, "r") as fd:
            data = fd.read().strip()
            seqno = int(data) if data != "" else 1

        return seqno

    async def expose(
        self,
        tile_data: dict | None = None,
        show_progress: bool = False,
        **kwargs,
    ):
        """Exposes the spectrographs."""

        count: int = kwargs.pop("count", 1)
        exposure_time: float = kwargs.pop("exposure_time", 10)

        exp_nos = []

        for _ in range(count):
            seqno = self.get_seqno()
            log.info(f"Taking spectrograph exposure {seqno}.")

            await self.reset()

            if tile_data is not None:
                header = json.dumps(tile_data)
            else:
                header = None

            if show_progress:
                timer = tqdm_timer(exposure_time + 45)
            else:
                timer = None

            try:
                await self._send_command_all(
                    "expose",
                    exposure_time=exposure_time,
                    seqno=seqno,
                    header=header,
                    **kwargs,
                )
                exp_nos.append(seqno)
            except Exception as err:
                with suppress(asyncio.CancelledError):
                    if timer:
                        await timer

                raise GortError(f"Exposure failed with error {err}")

        return exp_nos

    async def update_status(self):
        """Update the status fo all the spectrographs."""

        await asyncio.gather(*[spec.update_status() for spec in self.values()])

    async def reset(self):
        """Reset the spectrographs."""

        await self._send_command_all("reset")

    async def calibrate(self):
        """Runs the calibration sequence."""

        # TODO: add some checks. Confirm HDs are open, specs connected, etc.

        cal_config = config["specs"]["calibration"]

        log.info("Moving telescopes to position.")
        await self.gort.telescope.goto_named_position(cal_config["position"])

        calib_nps = self.gort.nps[cal_config["lamps_nps"]]
        lamps_config = cal_config["lamps"]

        # Turn off all lamps.
        log.info("Checking that all lamps are off.")
        for lamp in lamps_config:
            await calib_nps.off(lamp)

        for lamp in lamps_config:
            warmup = lamps_config[lamp]["warmup"]
            flavour = lamps_config[lamp]["flavour"]
            log.info(f"Warming up lamp {lamp} for {warmup} seconds.")
            await calib_nps.on(lamp)
            await asyncio.sleep(warmup)
            for exp_time in lamps_config[lamp]["exposure_times"]:
                log.info(f"Exposing for {exp_time} seconds.")
                await self.gort.spec.expose(flavour=flavour, exposure_time=exp_time)
            log.info(f"Turning off {lamp}.")
            await calib_nps.off(lamp)

        log.info("Taking biases.")
        nbias = cal_config["biases"]["count"]
        for _ in range(nbias):
            await self.gort.spec.expose(flavour="bias")

        log.info("Taking darks.")
        ndarks = cal_config["darks"]["count"]
        for _ in range(ndarks):
            await self.gort.spec.expose(
                flavour="dark",
                exposure_time=cal_config["darks"]["exposure_time"],
            )
