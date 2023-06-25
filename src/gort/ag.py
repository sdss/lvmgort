#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-25
# @Filename: ag.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from gort.gort import GortDevice, GortDeviceSet


class AG(GortDevice):
    """Class representing an AG camera."""

    async def status(self):
        """Returns the status of the AG."""

        return await self.actor.commands.status()


class AGSet(GortDeviceSet[AG]):
    """A set of auto-guiders."""

    __DEVICE_CLASS__ = AG
