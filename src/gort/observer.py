#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-09
# @Filename: observer.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import logging
import re

from typing import TYPE_CHECKING

from gort.exceptions import GortObserverError
from gort.tools import register_observation


if TYPE_CHECKING:
    from gort.gort import Gort
    from gort.tile import Tile


__all__ = ["GortObserver"]


class GortObserver:
    """A class to handle tile observations.

    Parameters
    ----------
    gort
        The instance of `.Gort` used to communicate with the devices.
    tile
        The `.Tile` with the information about the observation.
    mask_positions_pattern
        The ``spec`` fibre mask positions to use.

    """

    def __init__(self, gort: Gort, tile: Tile, mask_positions_pattern: str = "P1-*"):
        self.gort = gort
        self.tile = tile

        self.mask_positions = self._get_mask_positions(mask_positions_pattern)

        self.guide_task: asyncio.Future | None = None

    async def slew(self):
        """Slew to the telescope fields."""

        cotasks = []

        # Stops guiders.
        await self.gort.guiders.stop(now=True)

        # Slew telescopes.
        self.write_to_log(f"Slewing to tile_id={self.tile.tile_id}.", level="info")

        sci = (self.tile.sci_coords.ra, self.tile.sci_coords.dec)
        self.write_to_log(f"Science: {str(self.tile.sci_coords)}")

        spec = None
        if self.tile.spec_coords and len(self.tile.spec_coords) > 0:
            spec = (self.tile.spec_coords[0].ra, self.tile.spec_coords[0].dec)
            self.write_to_log(f"Spec: {self.tile.spec_coords[0]}")

        sky = {}
        for skytel in ["SkyE", "SkyW"]:
            if skytel.lower() in self.tile.sky_coords:
                sky_coords_tel = self.tile.sky_coords[skytel.lower()]
                sky[skytel.lower()] = (sky_coords_tel.ra, sky_coords_tel.dec)
                self.write_to_log(f"{skytel}: {sky_coords_tel}")

        cotasks.append(
            self.gort.telescopes.goto(
                sci=sci,
                spec=spec,
                skye=sky.get("skye", None),
                skyw=sky.get("skyw", None),
            )
        )

        # Move fibsel to first position.
        fibsel = self.gort.telescopes.spec.fibsel
        cotasks.append(fibsel.move_to_position(self.mask_positions[0]))

        # Execute.
        await asyncio.gather(*cotasks)

    async def acquire(
        self,
        guide_tolerance: float = 3,
        timeout: float = 180,
        min_skies: int = 1,
        require_spec: bool = True,
    ):
        """Acquires the field in all the telescopes. Blocks until then.

        Parameters
        ----------
        guide_tolerance
            The guide tolerance in arcsec. A telescope will not be considered
            to be guiding if its separation to the commanded field is larger
            than this value.
        timeout
            The maximum time allowed for acquisition. In case of timeout
            the acquired fields are evaluated and an exception is
            raised if the acquisition failed.
        min_skies
            Minimum number of skies required to consider acquisition successful.
        require_spec
            Whether to requiere the ``spec`` telescope to be guiding.

        Raises
        ------
        GortObserverError
            If the acquisition failed or the minimum required telescopes are
            not guiding.

        """

        # Determine telescopes on which to guide.
        telescopes = ["sci"]
        n_skies = 0
        if "skye" in self.tile.sky_coords:
            telescopes.append("skye")
            n_skies += 1
        if "skyw" in self.tile.sky_coords:
            telescopes.append("skyw")
            n_skies += 1
        if len(self.tile.spec_coords) > 0:
            telescopes.append("spec")

        if require_spec and "spec" not in telescopes:
            raise GortObserverError("spec pointing not defined.", error_code=801)
        if n_skies < min_skies:
            raise GortObserverError("Not enough sky positions defined.", error_code=801)

        # Start guide loop.
        self.guide_task = asyncio.gather(
            *[
                self.gort.guiders[tel].guide(
                    guide_tolerance=guide_tolerance,
                    pixel=self.mask_positions[0] if tel == "spec" else None,
                )
                for tel in telescopes
            ]
        )

        # Wait until convergence.
        guide_status = await asyncio.gather(
            *[
                self.gort.guiders[tel].wait_until_guiding(
                    guide_tolerance=guide_tolerance,
                    timeout=timeout,
                )
                for tel in telescopes
            ]
        )

        try:
            n_skies = 0
            for ii, tel in enumerate(telescopes):
                is_guiding = guide_status[ii][0]
                if tel == "sci" and not is_guiding:
                    raise GortObserverError(
                        "Science telescope is not guiding.",
                        error_code=801,
                    )
                if tel == "spec" and not is_guiding:
                    if require_spec:
                        raise GortObserverError(
                            "Spec telescope is not guiding.",
                            error_code=801,
                        )
                    else:
                        self.write_to_log("Spec telescope is not guiding", "warning")
                if "sky" in tel:
                    if is_guiding:
                        n_skies += 1
                    else:
                        self.write_to_log(f"{tel} telescope is not guiding.", "warning")

            if n_skies < min_skies:
                raise GortObserverError(
                    "Not enough sky telescopes guiding.",
                    error_code=801,
                )

        finally:
            self.write_to_log("Stopping guide loops.", "warning")
            await self.gort.guiders.stop()

    async def expose(self):
        """Starts exposing the spectrographs."""

        tile_id = self.tile.tile_id
        dither_pos = self.tile.dither_position

        exp_tile_data = {
            "tile_id": (tile_id, "The tile_id of this observation"),
            "dpos": (dither_pos, "Dither position"),
        }
        exp_nos = await self.gort.specs.expose(
            tile_data=exp_tile_data,
            show_progress=True,
        )

        if len(exp_nos) < 1:
            raise ValueError("No exposures to be registered.")

        self.write_to_log("Registering observation.")
        registration_payload = {
            "dither": dither_pos,
            "tile_id": tile_id,
            "jd": 0,
            "seeing": 10,
            "standards": [],
            "skies": [],
            "exposure_no": exp_nos[0],
        }
        self.write_to_log(f"Registration payload {registration_payload}")
        await register_observation(registration_payload)
        self.write_to_log("Registration complete.")

    def write_to_log(
        self,
        message: str,
        level: str = "debug",
        header: str | None = None,
    ):
        """Writes a message to the log with a custom header.

        Parameters
        ----------
        message
            The message to log.
        level
            The level to use for logging: ``'debug'``, ``'info'``, ``'warning'``, or
            ``'error'``.
        header
            The header to prepend to the message. By default uses the class name.

        """

        if header is None:
            header = f"({self.__class__.__name__}) "

        message = f"{header}{message}"

        level = logging.getLevelName(level.upper())
        assert isinstance(level, int)

        self.gort.log.log(level, message)

    def _get_mask_positions(self, pattern: str):
        """Returns mask positions sorted by motor steps."""

        mask_config = self.gort.config["telescopes"]["mask_positions"]
        all_positions = self.gort.telescopes.spec.fibsel.list_positions()
        positions = [pos for pos in all_positions if re.match(pattern, pos)]

        return sorted(positions, key=lambda p: mask_config[p])
