#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-19
# @Filename: robotic.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)


from gort import log
from gort.gort import Gort
from gort.tools import register_observation


__all__ = ["observe_tile"]


async def observe_tile(
    gort: Gort | None = None,
    tile_id: int | None = None,
    verbose=False,
):
    """Performs all the operations necessary to observe a tile.

    Parameters
    ----------
    gort
        A `.Gort` instance. If not provided, a new one will be created.
    tile_id
        The ``tile_id`` to observe. If not provided, observes the next tile
        suggested by the scheduler.
    verbose
        Be chatty.

    """

    if not gort:
        gort = await Gort().init()

    if verbose:
        gort.set_verbosity("debug")

    tile_id_data = await gort.telescopes.goto_tile_id(tile_id)

    tile_id = tile_id_data["tile_id"]
    dither_pos = tile_id_data["dither_pos"]

    exp_tile_data = {
        "tile_id": (tile_id, "The tile_id of this observation"),
        "dpos": (dither_pos, "Dither position"),
    }
    exp_nos = await gort.specs.expose(tile_data=exp_tile_data, show_progress=True)

    if len(exp_nos) < 1:
        raise ValueError("No exposures to be registered.")

    log.info("Registering observation.")
    registration_payload = {
        "dither": dither_pos,
        "tile_id": tile_id,
        "jd": tile_id_data["jd"],
        "seeing": 10,
        "standards": tile_id_data["standard_pks"],
        "skies": tile_id_data["sky_pks"],
        "exposure_no": exp_nos[0],
    }
    log.debug(f"Registration payload {registration_payload}")
    await register_observation(registration_payload)
    log.debug("Registration complete.")
