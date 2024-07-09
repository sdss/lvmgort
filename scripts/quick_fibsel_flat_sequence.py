#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-06-06
# @Filename: quick_fibsel_flat_sequence.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from gort import Gort


async def quick_fibsel_flat_sequence(time_per_fibre: float = 30, n_loops: int = 1):
    """Takes a single quartz flat exposure with all the spec fibres."""

    g = await Gort(verbosity="debug").init()

    await g.telescopes.spec.home(home_fibsel=True)
    await g.telescopes.spec.goto_named_position("calibration")

    await g.nps.calib.on("quartz")

    fibsel_positions_ordered = [
        "P1-2",
        "P1-1",
        "P1-12",
        "P1-11",
        "P1-10",
        "P1-9",
        "P1-8",
        "P1-7",
        "P1-6",
        "P1-5",
        "P1-4",
        "P1-3",
    ]

    for _ in range(n_loops):
        exposure_task = asyncio.create_task(
            g.specs.expose(
                time_per_fibre * 12 + 60,
                flavour="flat",
            )
        )

        for fibre in fibsel_positions_ordered:
            await g.telescopes.spec.fibsel.move_to_position(fibre)

            print(f"Exposing {fibre}.")
            await asyncio.sleep(time_per_fibre)

        await g.telescopes.spec.fibsel.move_relative(500)
        await exposure_task

    await g.nps.calib.off("quartz")


if __name__ == "__main__":
    asyncio.run(quick_fibsel_flat_sequence(n_loops=2))
