#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-15
# @Filename: spec.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from trurl import config, log
from trurl.core import TrurlDevice, TrurlDeviceSet


if TYPE_CHECKING:
    from trurl.core import ActorReply
    from trurl.trurl import Trurl


class Spectrograph(TrurlDevice):
    """Class representing an LVM spectrograph functionality."""

    def __init__(self, trurl: Trurl, name: str, actor: str, **kwargs):
        super().__init__(trurl, name, actor)

        self.status = {}

    async def update_status(self):
        """Retrieves the status of the telescope."""

        reply: ActorReply = await self.actor.commands.status()
        self.status = reply.flatten()

        return self.status

    async def expose(self, **kwargs):
        """Exposes the spectrograph."""

        await self.actor.commands.expose(**kwargs)


class SpectrographSet(TrurlDeviceSet[Spectrograph]):
    """A set of LVM spectrographs."""

    __DEVICE_CLASS__ = Spectrograph

    def get_seqno(self):
        """Returns the next exposure sequence number."""

        next_exposure_number_path = config["specs"]["nextExposureNumber"]
        with open(next_exposure_number_path, "r") as fd:
            data = fd.read().strip()
            seqno = int(data) if data != "" else 1

        return seqno

    async def expose(self, specs: list[str] | None = None, **kwargs):
        """Exposes the spectrographs."""

        seqno = self.get_seqno()
        log.info(f"Taking exposure {seqno}.")

        await self.reset()

        if specs is None:
            await self._send_command_all("expose", seqno=seqno, **kwargs)

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
        await self.trurl.telescopes.goto_named_position(cal_config["position"])

        calib_nps = self.trurl.nps[cal_config["lamps_nps"]]
        lamps_config = cal_config["lamps"]

        # Turn off all lamps.
        log.info("Checking that all lamps are off.")
        for lamp in lamps_config:
            await calib_nps.off(lamp)

        for lamp in lamps_config:
            warmup = lamps_config[lamp]["warmup"]
            log.info(f"Warming up lamp {lamp} for {warmup} seconds.")
            await calib_nps.on(lamp)
            await asyncio.sleep(warmup)
            for exp_time in lamps_config[lamp]["exposure_times"]:
                log.info(f"Exposing for {exp_time} seconds.")
                await self.trurl.specs.expose(flavour="arc", exposure_time=exp_time)
            log.info(f"Turning off {lamp}.")
            await calib_nps.off(lamp)

        log.info("Taking biases.")
        nbias = cal_config["biases"]["count"]
        for _ in range(nbias):
            await self.trurl.specs.expose(flavour="bias")

        log.info("Taking darks.")
        ndarks = cal_config["darks"]["count"]
        for _ in range(ndarks):
            await self.trurl.specs.expose(
                flavour="dark",
                exposure_time=cal_config["darks"]["exposure_time"],
            )
