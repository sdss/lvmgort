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
from gort.tile import Coordinates
from gort.tools import cancel_task
from gort.transforms import fibre_slew_coordinates


if TYPE_CHECKING:
    from gort.exposure import Exposure
    from gort.gort import Gort
    from gort.tile import Tile


__all__ = ["GortObserver"]


class GortObserver:
    """A class to handle tile observations.

    Parameters
    ----------
    gort
        The instance of :obj:`.Gort` used to communicate with the devices.
    tile
        The :obj:`.Tile` with the information about the observation.
    mask_positions_pattern
        The ``spec`` fibre mask positions to use.

    """

    def __init__(self, gort: Gort, tile: Tile, mask_positions_pattern: str = "P1-*"):
        self.gort = gort
        self.tile = tile

        self.mask_positions = self._get_mask_positions(mask_positions_pattern)

        self.guide_task: asyncio.Future | None = None

    def __repr__(self):
        return f"<GortObserver (tile_id={self.tile.tile_id})>"

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
            first_spec = self.tile.spec_coords[0]

            # For spec we slew to the fibre with which we'll observe first.
            # This should save a bit of time converging.
            spec_target = (first_spec.ra, first_spec.dec)
            spec = fibre_slew_coordinates(*spec_target, self.mask_positions[0])

            self.write_to_log(f"Spec: {first_spec} on {self.mask_positions[0]}")

        sky = {}
        for skytel in ["SkyE", "SkyW"]:
            if skytel.lower() in self.tile.sky_coords:
                sky_coords_tel = self.tile.sky_coords[skytel.lower()]
                if sky_coords_tel is not None:
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

    async def acquire(self, guide_tolerance: float = 3, timeout: float = 180):
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

        Raises
        ------
        GortObserverError
            If the acquisition failed or the minimum required telescopes are
            not guiding.

        """

        # Determine telescopes on which to guide.

        guide_coros = []
        guide_on_telescopes: list[str] = []
        n_skies = 0
        for tel in ["sci", "skye", "skyw", "spec"]:
            coords = self.tile[tel]

            if coords is None or (isinstance(coords, list) and len(coords) == 0):
                continue

            if tel == "spec" and isinstance(coords, list):
                coords = coords[0]
            assert isinstance(coords, Coordinates)

            guide_on_telescopes.append(tel)
            if "sky" in tel:
                n_skies += 1

            # Pixel in the MF on which to guide. Always None/central pixel
            # except for sci if defined. For spec we guide on the pixel of the
            # first fibre/mask position.
            pixel = coords._mf_pixel if tel != "spec" else self.mask_positions[0]

            guide_coros.append(
                self.gort.guiders[tel].guide(
                    ra=coords.ra,
                    dec=coords.dec,
                    guide_tolerance=guide_tolerance,
                    pixel=pixel,
                )
            )

        if "spec" not in guide_on_telescopes:
            self.write_to_log("No standards defined. Blocking fibre mask.", "warning")
            await self.gort.telescopes.spec.fibsel.move_relative(500)

        if n_skies == 0:
            self.write_to_log("No sky positions defined.", "warning")

        # Start guide loop.
        self.guide_task = asyncio.gather(*guide_coros)

        await asyncio.sleep(5)

        # Wait until convergence.
        self.write_to_log("Waiting for guiders to converge.")
        guide_status = await asyncio.gather(
            *[
                self.gort.guiders[tel].wait_until_guiding(timeout=timeout)
                for tel in guide_on_telescopes
            ]
        )

        has_timedout = any([gs[3] for gs in guide_status])
        if has_timedout:
            self.write_to_log("Some acquisitions timed out.", "warning")
            # TODO: we need to stop guiders that timed out.

        try:
            for ii, tel in enumerate(guide_on_telescopes):
                is_guiding = guide_status[ii][0]
                if tel == "sci" and not is_guiding:
                    raise GortObserverError(
                        "Science telescope is not guiding.",
                        error_code=801,
                    )
                if tel == "spec" and not is_guiding:
                    raise GortObserverError(
                        "Spec telescope is not guiding.",
                        error_code=801,
                    )
                if "sky" in tel and not is_guiding:
                    self.write_to_log(f"{tel} telescope is not guiding.", "warning")

        except Exception:
            self.write_to_log("Stopping guide loops.", "warning")
            await self.gort.guiders.stop()
            raise

        self.write_to_log("All telescopes are now guiding.")

    async def expose(
        self,
        exposure_time: float = 900.0,
        show_progress: bool | None = None,
        iterate_over_standards: bool = True,
        count: int = 1,
        **kwargs,
    ):
        """Starts exposing the spectrographs.

        Parameters
        ----------
        exposure_time
            The length of the exposure in seconds.
        show_progress
            Displays a progress bar with the elapsed exposure time.
        iterate_over_standards
            Whether to move the spec telescope during intergration
            to observe various standard stars in different fibres.
        count
            Number of exposures. If ``iterate_over_standards=True``, a
            full sequence of standards will be observed during each
            exposure.
        kwargs
            Other arguments to pass to :obj:`.SpectrographSet.expose`.

        """

        tile_id = self.tile.tile_id
        dither_pos = self.tile.dither_position

        if "object" not in kwargs:
            kwargs["object"] = self.tile.object

        header = await self._get_header()

        exposures: list[Exposure] = []
        standard_task: asyncio.Task | None = None

        for nexp in range(1, count + 1):
            self.write_to_log(
                f"Starting {exposure_time:.1f} s exposure ({nexp}/{count}).",
                "info",
            )

            if iterate_over_standards:
                standard_task = asyncio.create_task(
                    self._iterate_over_mask_positions(exposure_time)
                )

            exposure = await self.gort.specs.expose(
                exposure_time=exposure_time,
                header=header,
                show_progress=show_progress,
                count=1,
                **kwargs,
            )
            assert not isinstance(exposure, list)

            exposures.append(exposure)

            await cancel_task(standard_task)
            await exposure.register_observation(tile_id=tile_id, dither_pos=dither_pos)

        if len(exposures) == 1:
            return exposures[0]
        else:
            return exposures

    async def finish_observation(self):
        """Finishes the observation, stops the guiders, etc."""

        self.write_to_log("Finishing observation.", "info")

        if self.guide_task is not None and not self.guide_task.done():
            await self.gort.guiders.stop()
            await self.guide_task

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

        # Time to acquire a standard.
        ACQ_PER_STD = 30

        spec_coords = self.tile.spec_coords

        # If we have zero or one standards, do nothing. The spec telescope
        # is already pointing to the first mask position.
        if len(spec_coords) <= 1:
            return

        guider_pixels: dict[str, tuple[float, float]]
        guider_pixels = self.gort.config["guiders"]["devices"]["spec"]["named_pixels"]

        # Time at which the exposure began.
        t0 = time()

        # Time to spend on each mask position.
        n_stds = len(spec_coords)

        # Calculate the time to actually be on target, taking into account
        # how long we expect to take acquiring.
        time_per_position = exposure_time / n_stds - ACQ_PER_STD
        if time_per_position < 0:
            self.write_to_log(
                "Exposure time is too short to observe this "
                "many standards. I will do what I can."
            )
            time_per_position = exposure_time / n_stds

        # Time at which we started observing the last standard.
        t0_last_std = t0

        # Number of standards observed.
        n_observed = 1

        # Index of current standard being observed.
        current_std_idx = 0

        while True:
            await asyncio.sleep(1)

            # We consider than if there is less 2 * ACQ_PER_STD left in the
            # exposure there is no point in going to the next standard.
            t_now = time()
            if t_now - t0 > exposure_time - 2 * ACQ_PER_STD:
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

                # Pixel on the MF corresponding to the new fibre/mask hole on
                # which to guide. We use this tabulated list instead of
                # offset_to_master_frame_pixel() because the latter coordinates
                # are less precise as they do not include IFU rotation and more
                # precise metrology.
                new_guider_pixel = guider_pixels[new_mask_position]

                self.write_to_log(
                    f"Moving to standard #{current_std_idx+1} ({new_coords}) "
                    f"on fibre {new_mask_position}.",
                    "info",
                )

                # Finish guiding on spec telescope.
                self.write_to_log("Stopping guiding on spec telescope.")
                await self.gort.guiders["spec"].stop()

                # TODO: some of these things can be concurrent.
                spec_tel = self.gort.telescopes.spec

                # Moving the mask to an intermediate position while we move around.
                await spec_tel.fibsel.move_relative(500)

                # Slew to new coordinates. We actually slew to the coordinates
                # that make the new star close to the fibre that will observe it.
                slew_ra, slew_dec = fibre_slew_coordinates(
                    new_coords.ra,
                    new_coords.dec,
                    new_mask_position,
                )
                await spec_tel.goto_coordinates(ra=slew_ra, dec=slew_dec)

                # Start to guide. Note that here we use the original coordinates
                # of the star along with the pixel on the master frame on which to
                # guide. See the note in fibre_slew_coordinates().
                self.write_to_log("Starting to guide on spec telescope.")
                asyncio.create_task(
                    self.gort.guiders.spec.guide(
                        ra=new_coords.ra,
                        dec=new_coords.dec,
                        guide_tolerance=5,
                        pixel=new_guider_pixel,
                    )
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

    async def _get_header(self):
        """Returns the extra header dictionary from the tile data."""

        tile_id = self.tile.tile_id
        dither_pos = self.tile.dither_position

        header = {
            "tile_id": (tile_id or -999, "The tile_id of this observation"),
            "dpos": (dither_pos, "Dither position"),
            "poscira": round(self.tile.sci_coords.ra, 6),
            "poscide": round(self.tile.sci_coords.dec, 6),
        }

        if self.tile["skye"]:
            header.update(
                {
                    "poskyera": round(self.tile.sky_coords["skye"].ra, 6),
                    "poskyede": round(self.tile.sky_coords["skye"].dec, 6),
                }
            )

        if self.tile["skyw"]:
            header.update(
                {
                    "poskywra": round(self.tile.sky_coords["skyw"].ra, 6),
                    "poskywde": round(self.tile.sky_coords["skyw"].dec, 6),
                }
            )

        return header.copy()
