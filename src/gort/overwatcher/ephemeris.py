#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: ephemeris.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

import httpx

from sdsstools import get_sjd

from gort.overwatcher import OverwatcherModule


class EphemerisOverwatcher(OverwatcherModule):
    """Monitors ephemeris data."""

    name = "ephemeris"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.sjd = get_sjd("LCO")
        self.ephemeris: dict | None = None

    async def run(self):
        """Starts the ephemeris monitor."""

        if self.is_running:
            self.gort.log.warning("Ephemeris monitor is already running.")
            return await super().run()

        self.tasks["ephemeris"] = asyncio.create_task(self.monitor_ephemeris())

        return await super().run()

    async def monitor_ephemeris(self):
        """Monitors SJD change and keeps ephemeris updated."""

        while True:
            new_sjd = get_sjd("LCO")

            if self.ephemeris is None or new_sjd != self.sjd:
                self.sjd = new_sjd

                try:
                    self.ephemeris = await self.get_ephemeris()
                except Exception as err:
                    self.gort.log.error(f"Failed to get ephemeris data: {err!r}")
                    await asyncio.sleep(10)
                    continue

            await asyncio.sleep(600)

    async def get_ephemeris(self, sjd: int | None = None):
        """Returns ephemeris data."""

        sjd = sjd or self.sjd

        async with httpx.AsyncClient(
            base_url="http://localhost:8085",
            follow_redirects=True,
            timeout=10,
        ) as client:
            response = await client.get("/ephemeris", params={"sjd": sjd})

            if response.status_code != 200:
                raise ValueError("Failed to get ephemeris data.")

        return response.json()
