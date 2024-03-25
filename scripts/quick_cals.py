#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import random

from gort import Gort


async def quick_cals():
    g = await Gort(verbosity="debug").init()

    await g.cleanup()  # KK added Jan 30, 2024

    print("Moving telescopes to point to the calibration screen.")
    await g.telescopes.goto_named_position("calibration")
    #    await g.telescopes.goto_named_position("selfie")

    ########################
    # Arcs
    ########################

    print("Turning on the HgNe lamp.")
    await g.nps.calib.on("HgNe")

    print("Turning on the Ne lamp.")
    await g.nps.calib.on("Neon")

    print("Turning on the Argon lamp.")
    await g.nps.calib.on("Argon")

    print("Turning on the Xenon lamp.")
    await g.nps.calib.on("Xenon")

    print("Waiting 180 seconds for the lamps to warm up.")
    await asyncio.sleep(180)

    fiber = random.randint(1, 12)  # select random fibre on std telescope
    fiber_str = f"P1-{fiber}"
    print(f"Taking {fiber_str} exposure.")
    await g.telescopes.spec.fibsel.move_to_position(fiber_str)

    exp = await g.specs.expose(
        10, show_progress=True, flavour="arc", header={"CALIBFIB": f"P1-{fiber}"}
    )
    exp = await g.specs.expose(
        50, show_progress=True, flavour="arc", header={"CALIBFIB": f"P1-{fiber}"}
    )
    print("Exposures are:")
    print(exp.get_files())

    print("Turning off all lamps.")
    await g.nps.calib.all_off()

    ########################
    # Flats
    ########################

    print("Turning on the Quartz lamp.")
    await g.nps.calib.on("Quartz")

    print("Waiting 120 seconds for the lamp to warm up.")
    await asyncio.sleep(120)
    expQuartz = 10

    exp = await g.specs.expose(
        expQuartz,
        show_progress=True,
        flavour="flat",
        header={"CALIBFIB": f"P1-{fiber}"},
    )
    print("Exposures are:")
    print(exp.get_files())

    print("Turning off the Quartz lamp.")
    await g.nps.calib.all_off()

    ########################
    print("Turning on the LDLS lamp.")
    await g.nps.calib.on("LDLS")

    print("Waiting 300 seconds for the lamp to warm up.")
    await asyncio.sleep(300)

    expLDLS = 150

    exp = await g.specs.expose(
        expLDLS, show_progress=True, flavour="flat", header={"CALIBFIB": f"P1-{fiber}"}
    )
    print("Exposures are:")
    print(exp.get_files())

    print("Turning off the LDLS lamp.")
    await g.nps.calib.all_off()


if __name__ == "__main__":
    asyncio.run(quick_cals())
