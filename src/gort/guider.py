#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-18
# @Filename: nps.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from gort.exceptions import GortError
from gort.gort import GortDevice, GortDeviceSet


if TYPE_CHECKING:
    from gort.gort import GortClient


class Guider(GortDevice):
    """Class representing a guider."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

        try:
            self.ag = self.gort.ags[self.name]
        except KeyError:
            self.ag = None

    async def focus(
        self,
        inplace=False,
        guess: float = 40,
        step_size: float = 0.5,
        steps: int = 7,
        exposure_time: float = 5.0,
    ):
        """Focus the telescope."""

        # Send telescopes to zenith.
        if not inplace:
            self.write_to_log("Moving telescope to zenith.")
            await self.gort.telescopes[self.name].goto_named_position(
                "zenith",
                altaz_tracking=True,
            )

        try:
            await self.actor.commands.focus(
                reply_callback=self.print_reply,
                guess=guess,
                step_size=step_size,
                steps=steps,
                exposure_time=exposure_time,
            )
        except GortError as err:
            self.write_to_log(f"Failed focusing with error {err}", level="error")


class GuiderSet(GortDeviceSet[Guider]):
    """A set of telescope guiders."""

    __DEVICE_CLASS__ = Guider

    async def take_darks(self):
        """Takes AG darks."""

        # Move telescopes to park to prevent light, since we don't have shutters.
        # We use goto_named_position to prevent disabling the telescope and having
        # to rehome.
        self.write_to_log("Moving telescopes to park position.", level="info")
        await self.gort.telescopes.goto_named_position("park")

        # Take darks.
        self.write_to_log("Taking darks.", level="info")

        cmds = []
        for ag in self.values():
            cmds.append(
                ag.actor.commands.guide.commands.expose(
                    flavour="dark",
                    reply_callback=ag.print_reply,
                )
            )

        if len(cmds) > 0:
            await asyncio.gather(*cmds)

    async def focus(
        self,
        inplace=False,
        guess: float = 40,
        step_size: float = 0.5,
        steps: int = 7,
        exposure_time: float = 5.0,
    ):
        """Focus all the telescopes."""

        jobs = [
            ag.focus(
                inplace=inplace,
                guess=guess,
                step_size=step_size,
                steps=steps,
                exposure_time=exposure_time,
            )
            for ag in self.values()
        ]
        await asyncio.gather(*jobs)
