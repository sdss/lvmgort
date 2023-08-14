#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-08-14
# @Filename: telemetry.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING

from gort.gort import GortDevice, GortDeviceSet


if TYPE_CHECKING:
    from gort import ActorReply
    from gort.gort import GortClient


__all__ = ["Telemetry", "TelemetrySet"]


class Telemetry(GortDevice):
    """Telemetry sensors."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

    async def status(self):
        """Retrieves the status of sensors."""

        reply: ActorReply = await self.actor.commands.status()
        return reply.flatten()


class TelemetrySet(GortDeviceSet[Telemetry]):
    """A set of telemetry sensors."""

    __DEVICE_CLASS__ = Telemetry
    __DEPLOYMENTS__ = ["lvmtelemetry"]
