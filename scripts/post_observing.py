#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-10-28
# @Filename: post_observing.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from gort.overwatcher.helpers import post_observing
from gort.tools import get_gort_client


async def post_observing_script():
    """Runs the post-observing tasks."""

    async with get_gort_client() as gort:
        await post_observing(gort)

        # Disable the overwatcher.
        cmd = await gort.send_command("lvm.overwatcher", "disable")
        if cmd.status.did_fail:
            gort.log.error("Failed to disable the overwatcher.")
        else:
            gort.log.info("Overwatcher has been disabled.")


if __name__ == "__main__":
    asyncio.run(post_observing_script())
