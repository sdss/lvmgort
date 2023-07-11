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
from time import time

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
        await self.gort.guiders.stop()

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
        # TODO: do we need to pass the ra/dec coordinates of the target here?
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
        self.write_to_log("Waiting for guiders to converge.")
        guide_status = await asyncio.gather(
            *[
                self.gort.guiders[tel].wait_until_guiding(timeout=timeout)
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

    async def expose(
        self,
        exposure_time: float = 900.0,
        show_progress: bool = True,
        iterate_over_standards: bool = True,
    ):
        """Starts exposing the spectrographs.

        Parameters
        ----------
        exposure_time
            The lenght of the exposure in seconds.
        show_progress
            Displays a progress bar with the elapsed exposure time.
        iterate_over_standards
            Whether to move the spec telescope during intergration
            to observe various standard stars in different fibres.

        """

        standard_task: asyncio.Task | None = None
        if iterate_over_standards:
            standard_task = asyncio.create_task(
                self._iterate_over_mask_positions(exposure_time)
            )

        tile_id = self.tile.tile_id
        dither_pos = self.tile.dither_position

        exp_tile_data = {
            "tile_id": (tile_id or -999, "The tile_id of this observation"),
            "dpos": (dither_pos, "Dither position"),
        }

        exp_nos = await self.gort.specs.expose(
            exposure_time=exposure_time,
            tile_data=exp_tile_data,
            show_progress=show_progress,
        )

        if standard_task is not None and not standard_task.done():
            await standard_task

        if tile_id:
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

    async def _iterate_over_mask_positions(self, exposure_time: float):
        """Iterates over the fibre mask positions.

        Moving the ``spec`` telescope to each one of the standard star,
        acquires the new field, and adjusts the time to stay on each target.

        """

        # TODO: record how long we exposed on each standard and save that
        # information somewhere.

        spec_coords = self.tile.spec_coords

        # If we have zero or one standards, do nothing. The spec telescope
        # is already pointing to the first mask position.
        if len(spec_coords) <= 1:
            return

        # Time at which the exposure began.
        t0 = time()

        # Time to spend on each mask position.
        n_stds = len(spec_coords)
        time_per_position = exposure_time / n_stds

        # Time at which we started observing the last standard.
        t0_last_std = t0

        # Number of standards observed.
        n_observed = 1

        # Index of current standard being observed.
        current_std_idx = 0

        while True:
            await asyncio.sleep(1)

            # We consider than if there is less than 2 minutes left in the
            # exposure there is no point in going to the next standard.
            t_now = time()
            if t_now - t0 > exposure_time - 120:
                self.write_to_log("Exiting standard loop.")
                self.write_to_log(f"Standards observed: {n_observed}/{n_stds}.", "info")
                return

            # Time to move to another standard?
            if t_now - t0_last_std > time_per_position:
                # Check that we haven't run out standards. If so,
                # keep observing this one.
                if len(spec_coords) == current_std_idx + 1:
                    continue

                # Increase current index and get coordinates.
                current_std_idx += 1

                # New coordinates to observe.
                new_coords = spec_coords[current_std_idx]
                new_mask_position = self.mask_positions[current_std_idx]

                self.write_to_log(
                    f"Moving to standard #{current_std_idx+1} ({new_coords}) "
                    f"on fibre {new_mask_position}."
                )

                # Finish guiding on spec telescope.
                self.write_to_log("Stopping guiding on spec telescope.")
                await self.gort.guiders["spec"].stop()

                # TODO: some of these things can be concurrent.
                spec_tel = self.gort.telescopes.spec

                # Moving the mask to an intermediate position while we move around.
                await spec_tel.fibsel.move_relative(500)

                # Slew to new coordinates.
                # TODO: to speed up acquisition we should slew to the coordinates
                # of the fibre, or do an offset just before beginning to guide.
                await spec_tel.goto_coordinates(ra=new_coords.ra, dec=new_coords.dec)

                # Start to guide.
                self.write_to_log("Starting to guide on spec telescope.")
                await self.gort.guiders.spec.guide(
                    fieldra=new_coords.ra,
                    fielddec=new_coords.dec,
                    guide_tolerance=5,
                    pixel=new_mask_position,
                )

                result = await self.gort.guiders.spec.wait_until_guiding(timeout=60)
                if result[0] is False:
                    self.write_to_log(
                        "Failed to acquire standard position. Skipping.",
                        "warning",
                    )
                    continue

                self.write_to_log(f"Standard position {new_mask_position} acquired.")

                # Move mask to uncover fibre.
                await spec_tel.fibsel.move_to_position(new_mask_position)

                n_observed += 1
                t0_last_std = time()
