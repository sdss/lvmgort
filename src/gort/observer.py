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
from contextlib import contextmanager
from time import time

from typing import TYPE_CHECKING, Any

import numpy
import pandas
from astropy.time import Time

from gort.exceptions import ErrorCodes, GortObserverError
from gort.exposure import Exposure
from gort.maskbits import GuiderStatus
from gort.tile import Coordinates
from gort.tools import (
    build_guider_reply_list,
    cancel_task,
    insert_to_database,
    mark_exposure_bad,
    register_observation,
)
from gort.transforms import fibre_slew_coordinates


if TYPE_CHECKING:
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
        self.guider_monitor = GuiderMonitor(self)

        self.standards = Standards(self, tile, self.mask_positions)

        self.__current_exposure: Exposure | None = None
        self._n_exposures: int = 0

        self.overheads: dict[str, tuple[float, float]] = {}

    def __repr__(self):
        return f"<GortObserver (tile_id={self.tile.tile_id})>"

    @property
    def has_standards(self):
        """Returns :obj:`True` if standards will be observed."""

        return len(self.standards.standards) > 0

    async def slew(self):
        """Slew to the telescope fields."""

        tile = self.tile

        cotasks = []

        with self.register_overhead("slew:stop-guiders"):
            # Stops guiders.
            await self.gort.guiders.stop()

        # Slew telescopes.
        self.write_to_log(f"Slewing to tile_id={tile.tile_id}.", level="info")

        sci = (tile.sci_coords.ra, tile.sci_coords.dec, tile.sci_coords.pa)
        self.write_to_log(f"Science: {str(tile.sci_coords)}")

        spec = None
        if tile.spec_coords and len(tile.spec_coords) > 0:
            first_spec = tile.spec_coords[0]

            # For spec we slew to the fibre with which we'll observe first.
            # This should save a bit of time converging.
            spec = fibre_slew_coordinates(
                first_spec.ra,
                first_spec.dec,
                self.mask_positions[0],
                derotated=False,
            )

            self.write_to_log(f"Spec: {first_spec} on {self.mask_positions[0]}")

        sky = {}
        for skytel in ["SkyE", "SkyW"]:
            if skytel.lower() in tile.sky_coords:
                sky_coords_tel = tile.sky_coords[skytel.lower()]
                if sky_coords_tel is not None:
                    sky[skytel.lower()] = (sky_coords_tel.ra, sky_coords_tel.dec)
                    self.write_to_log(f"{skytel}: {sky_coords_tel}")

        # For sci we want to slew the k-mirror so that we can apply small positive
        # offsets without backlash. So we slew to the tile PA-stop_degs_before.
        kmirror_config = self.gort.config["telescopes"]["kmirror"]
        stop_degs_before = kmirror_config.get("stop_degs_before", {}).get("sci", 0.0)

        cotasks.append(
            self.gort.telescopes.goto(
                sci=sci,
                spec=spec,
                skye=sky.get("skye", None),
                skyw=sky.get("skyw", None),
                sci_km_stop_degs_before=stop_degs_before,
            )
        )

        # Move fibsel to first position.
        fibsel = self.gort.telescopes.spec.fibsel
        cotasks.append(fibsel.move_to_position(self.mask_positions[0]))

        # Execute.
        with self.register_overhead("slew:slew"):
            await asyncio.gather(*cotasks)

    async def acquire(self, guide_tolerance: float | None = None, timeout: float = 180):
        """Acquires the field in all the telescopes. Blocks until then.

        Parameters
        ----------
        guide_tolerance
            The guide tolerance in arcsec. A telescope will not be considered
            to be guiding if its separation to the commanded field is larger
            than this value. If `None`, default values from the configuration
            file are used.
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

        with self.register_overhead("acquisition:stop-guiders"):
            # Make sure we are not guiding or that the previous is on.
            if self.guide_task is not None and not self.guide_task.done():
                await self.gort.guiders.stop()
                await cancel_task(self.guide_task)

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

            guide_tolerance_tel = self.gort.config["observer"]["guide_tolerance"][tel]
            guide_tolerance_tel = guide_tolerance or guide_tolerance_tel

            guide_coros.append(
                self.gort.guiders[tel].guide(
                    ra=coords.ra,
                    dec=coords.dec,
                    pa=-coords.pa,  # -1 since k-mirror handiness is opposite to tile PA
                    guide_tolerance=guide_tolerance_tel,
                    pixel=pixel,
                )
            )

        if "spec" not in guide_on_telescopes:
            with self.register_overhead("acquisition:move-fibsel-500"):
                self.write_to_log("Not using spec: blocking fibre mask.", "warning")
                await self.gort.telescopes.spec.fibsel.move_relative(500)

        if n_skies == 0:
            self.write_to_log("No sky positions defined.", "warning")

        with self.register_overhead("acquisition:start-guide-loop"):
            # Start guide loop.
            await self.guider_monitor.restart()
            self.guide_task = asyncio.gather(*guide_coros)

        with self.register_overhead("acquisition:acquire"):
            await asyncio.sleep(2)

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

                if tel == "spec":
                    if not is_guiding:
                        raise GortObserverError(
                            "Spec telescope is not guiding.",
                            error_code=ErrorCodes.ACQUISITION_FAILED,
                        )
                    else:
                        await self.gort.guiders.spec.apply_corrections(False)

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
        count: int = 1,
        async_readout: bool = False,
        keep_guiding: bool = True,
        object: str | None = None,
        dither_position: int | None = None,
    ):
        """Starts exposing the spectrographs.

        Parameters
        ----------
        exposure_time
            The length of the exposure in seconds.
        show_progress
            Displays a progress bar with the elapsed exposure time.
        count
            Number of exposures. If ``iterate_over_standards=True``, a
            full sequence of standards will be observed during each
            exposure.
        async_readout
            Whether to wait for the readout to complete or return as soon
            as the readout begins. If :obj:`False`, the exposure is registered
            but the observation is not finished.
        keep_guiding
            If :obj:`True`, keeps the guider running after the last exposure.
        object
            The object name to be added to the header.
        dither_position
            The dither position. If :obj:`None`, uses the first dither position
            in the tile. Only relevant for exposure registration.

        Returns
        -------
        exposures
            Either a single `.Exposure` or a list of exposures if ``count>1``.

        """

        tile_id = self.tile.tile_id

        dither_position = dither_position or self.tile.sci_coords.dither_position

        if object is None:
            if self.tile.object:
                object = self.tile.object
            elif tile_id is not None and tile_id > 0:
                object = f"tile_id={tile_id}"

        exposures: list[Exposure] = []

        for nexp in range(1, count + 1):
            self.write_to_log(
                f"Starting {exposure_time:.1f} s exposure ({nexp}/{count}).",
                "info",
            )

            with self.register_overhead(f"expose:pre-exposure-{nexp}"):
                # Refresh guider data for this exposure.
                if self._n_exposures > 0:
                    await self.guider_monitor.restart()

                # Move fibre selector to the first position. Should be there unless
                # count > 1 in which case we need to move it back.
                await self.standards.start_iterating(exposure_time)

                exposure = Exposure(self.gort, flavour="object", object=object)
                self.__current_exposure = exposure

            exposure.hooks["pre-readout"].append(self._pre_readout)
            exposure.hooks["post-readout"].append(self._post_readout)

            with self.register_overhead(f"expose:integration-{nexp}"):
                await exposure.expose(
                    exposure_time=exposure_time,
                    show_progress=show_progress,
                    async_readout=True,
                )

            # TODO: this is a bit dangerous because we are registering the
            # exposure before  it's actually written to disk. Maybe we should
            # wait until _post_readout() to register, but then the scheduler
            # needs to be changed to not return the same tile twice.
            with self.register_overhead(f"exposure:register-exposure-{nexp}"):
                await self.register_exposure(
                    exposure,
                    tile_id=tile_id,
                    dither_position=dither_position,
                )

            if nexp == count and not keep_guiding:
                with self.register_overhead(f"expose:stop-guiders-{nexp}"):
                    await self.gort.guiders.stop()

            if nexp == count and async_readout:
                return exposure
            else:
                await exposure

            exposures.append(exposure)
            self._n_exposures += 1

        if len(exposures) == 1:
            return exposures[0]
        else:
            return exposures

    async def finish_observation(self):
        """Finishes the observation, stops the guiders, etc."""

        self.write_to_log("Finishing observation.", "info")

        with self.register_overhead("finish-observation"):
            # Should have been cancelled in _update_header(), but just in case.
            await self.standards.cancel()

            if self.guide_task is not None and not self.guide_task.done():
                await self.gort.guiders.stop()
                await self.guide_task

        tile_id = self.tile.tile_id
        if tile_id is None or tile_id <= 0:
            tile_id = None

        # Write overheads to database.
        payload = [
            {
                "observer_id": id(self),
                "tile_id": tile_id,
                "stage": name,
                "start_time": value[0],
                "end_time": value[0] + value[1],
                "duration": value[1],
            }
            for name, value in self.overheads.items()
        ]

        try:
            table_name = self.gort.config["database"]["tables"]["overhead"]
            insert_to_database(table_name, payload)
        except Exception as err:
            self.write_to_log(f"Failed saving overheads to database: {err}", "error")

    async def register_exposure(
        self,
        exposure: Exposure,
        tile_id: int | None = None,
        dither_position: int = 0,
    ):
        """Registers the exposure in the database."""

        if exposure._exposure_time is None:
            raise GortObserverError("Exposure time cannot be 'None'.")

        if exposure.flavour != "object":
            return

        standards: list[int] = []
        for stdn, row in self.standards.standards.iterrows():
            if row.observed == 1:
                pk = self.tile.spec_coords[stdn - 1].pk  # type:ignore
                if pk is not None:
                    standards.append(pk)

        skies = [sky.pk for sky in self.tile.sky_coords.values() if sky.pk is not None]

        self.write_to_log("Registering observation.", "info")
        registration_payload = {
            "dither": dither_position,
            "jd": exposure.start_time.jd,
            "seeing": -999.0,
            "standards": standards,
            "skies": skies,
            "exposure_no": exposure.exp_no,
            "exposure_time": exposure._exposure_time,
        }

        if tile_id is not None:
            registration_payload["tile_id"] = tile_id

        self.write_to_log(f"Registration payload {registration_payload}")

        try:
            await register_observation(registration_payload)
        except Exception as err:
            self.write_to_log(f"Failed registering exposure: {err}", "error")
        else:
            self.write_to_log("Registration complete.")

    async def set_dither_position(self, dither: int):
        """Reacquire science telescope for a new dither position."""

        valid_status = (
            GuiderStatus.GUIDING | GuiderStatus.DRIFTING | GuiderStatus.ACQUIRING
        )

        sci_status = self.gort.guiders.sci.status
        if sci_status is None or not (sci_status & valid_status):
            raise GortObserverError("sci guider must be active to set dither position.")

        self.tile.set_dither_position(dither)
        await self.gort.guiders.sci.set_pixel(self.tile.sci_coords._mf_pixel)

        # Wait until converges.
        with self.register_overhead(f"set-dither-position:acquire-{dither}"):
            await asyncio.sleep(2)
            self.write_to_log("Waiting for 'sci' guider to converge.")
            await self.gort.guiders.sci.wait_until_guiding(timeout=120)

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

    @contextmanager
    def register_overhead(self, name: str):
        """Measures and registers and overhead."""

        t0 = time()

        yield

        self.overheads[name] = (t0, time() - t0)

    def _get_mask_positions(self, pattern: str):
        """Returns mask positions sorted by motor steps."""

        mask_config = self.gort.config["telescopes"]["mask_positions"]
        all_positions = self.gort.telescopes.spec.fibsel.list_positions()
        positions = [pos for pos in all_positions if re.match(pattern, pos)]

        return sorted(positions, key=lambda p: mask_config[p])

    async def _pre_readout(self, header: dict[str, Any]):
        """Updates the exposure header with pointing and guiding information."""

        tile_id = self.tile.tile_id
        dither_pos = self.tile.sci_coords.dither_position

        tile_header = {
            "TILE_ID": (tile_id or -999, "The tile_id of this observation"),
            "DPOS": (dither_pos, "Dither position"),
            "POSCIRA": round(self.tile.sci_coords.ra, 6),
            "POSCIDE": round(self.tile.sci_coords.dec, 6),
            "POSCIPA": round(self.tile.sci_coords.pa, 4),
        }

        if self.tile["skye"]:
            tile_header.update(
                {
                    "POSKYERA": round(self.tile.sky_coords["skye"].ra, 6),
                    "POSKYEDE": round(self.tile.sky_coords["skye"].dec, 6),
                    "POSKYEPA": round(self.tile.sky_coords["skye"].pa, 4),
                    "SKYENAME": self.tile.sky_coords["skye"].name,
                }
            )

        if self.tile["skyw"]:
            tile_header.update(
                {
                    "POSKYWRA": round(self.tile.sky_coords["skyw"].ra, 6),
                    "POSKYWDE": round(self.tile.sky_coords["skyw"].dec, 6),
                    "POSKYWPA": round(self.tile.sky_coords["skyw"].pa, 4),
                    "SKYWNAME": self.tile.sky_coords["skyw"].name,
                }
            )

        header.update(tile_header)

        self.guider_monitor.update_data()
        header.update(self.guider_monitor.to_header())

        # At this point the shutter is closed so let's stop observing standards.
        # This also finishes updating the standards table.
        if self.has_standards:
            await self.standards.cancel()

            # There is some overhead between when we set t0 for the first standard
            # and when the exposure actually begins. This leads to the first standard
            # having longer exposure time than open shutter.
            if self.has_standards and self.__current_exposure is not None:
                start_time = self.__current_exposure.start_time.unix
                self.standards.standards.loc[1, "t0"] = start_time

            header.update(self.standards.to_header())

    async def _post_readout(self, exposure: Exposure):
        """Post exposure tasks."""

        tile_id = self.tile.tile_id
        dither_pos = self.tile.sci_coords.dither_position

        if exposure.error is True:
            if tile_id is not None:
                mark_exposure_bad(tile_id, dither_pos)

            self.write_to_log(
                "Exposure returned with errors. Tile-dither has been marked as bad.",
                "error",
            )


class GuiderMonitor:
    """Monitors guider exposures."""

    def __init__(self, observer: GortObserver):
        self.observer = observer
        self.gort = observer.gort

        self.guider_task: asyncio.Task | None = None
        self.guider_data: pandas.DataFrame | None = None

        self._current_data = []

    async def start(self):
        """Starts monitoring."""

        self.guider_task = asyncio.create_task(self._guider_monitor())

    async def stop(self):
        """Stops monitoring."""

        await cancel_task(self.guider_task)

    async def restart(self):
        """Restarts monitoring."""

        self.guider_data = None

        await self.stop()
        await self.start()

    async def _guider_monitor(self):
        """Monitors the guider data and build a data frame."""

        self._current_data = []

        task = asyncio.create_task(
            build_guider_reply_list(
                self.gort,
                self._current_data,
            )
        )

        try:
            while True:
                # Just keep the task running.
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            await cancel_task(task)
            self.update_data()

    def update_data(self):
        """Updates the guider data frame."""

        if len(self._current_data) > 0:
            # Build DF with all the frames.
            df = pandas.DataFrame.from_records(self._current_data)

            # Group by frameno, keep only non-NaN values.
            df = df.groupby(["frameno", "telescope"], as_index=False).apply(
                lambda g: g.bfill(axis=0).iloc[0, :]
            )

            # Remove NaN rows.
            df = df.loc[~numpy.isnan(df.frameno)]

            # Sort by frameno.
            df = df.sort_values("frameno")

            self.guider_data = df

    def to_header(self):
        """Returns a header with pointing and guiding information."""

        header: dict[str, Any] = {}

        if self.guider_data is not None:
            for tel in ["sci", "spec", "skye", "skyw"]:
                try:
                    tel_data = self.guider_data.loc[self.guider_data.telescope == tel]
                    # tel_data = tel_data.loc[tel_data["mode"] == "guide"]
                    if len(tel_data) < 2:
                        frame0 = None
                        framen = None
                    else:
                        frame0 = int(tel_data.frameno.min())
                        framen = int(tel_data.frameno.max())

                    header.update(
                        {
                            f"G{tel.upper()}FR0": (frame0, f"{tel} first guider frame"),
                            f"G{tel.upper()}FRN": (framen, f"{tel} last guider frame"),
                        }
                    )

                except Exception as err:
                    self.gort.specs.write_to_log(
                        f"Failed updating guider header information for {tel}: {err}",
                        "warning",
                    )
                    continue

        return header


class Standards:
    """Iterates over standards and monitors observed standards."""

    def __init__(self, observer: GortObserver, tile: Tile, mask_positions: list[str]):
        self.observer = observer
        self.gort = observer.gort

        self.tile = tile

        self.iterate_task: asyncio.Task | None = None

        self.current_standard: int = 1
        self.standards = self._get_frame()

    @property
    def mask_positions(self):
        """Returns the list of mask positions from `.GortObserver`."""

        return self.observer.mask_positions

    def _get_frame(self):
        """Constructs the standard data frame."""

        stdn = list(range(1, len(self.tile.spec_coords) + 1))
        source_id = [cc.source_id or -1 for cc in self.tile.spec_coords]
        ra = [cc.ra for cc in self.tile.spec_coords]
        dec = [cc.dec for cc in self.tile.spec_coords]

        default = [0] * len(stdn)

        df = pandas.DataFrame(
            {
                "n": pandas.Series(stdn, dtype=numpy.int16),
                "source_id": pandas.Series(source_id, dtype=numpy.int64),
                "ra": pandas.Series(ra, dtype=numpy.float64),
                "dec": pandas.Series(dec, dtype=numpy.float64),
                "acquired": pandas.Series(default, dtype=numpy.int16),
                "observed": pandas.Series(default, dtype=numpy.int16),
                "t0": pandas.Series(default, dtype=numpy.float64),
                "t1": pandas.Series(default, dtype=numpy.float64),
                "fibre": pandas.Series([""] * len(stdn), dtype=str),
            }
        )

        df.set_index("n", inplace=True)

        return df

    async def start_iterating(self, exposure_time: float) -> None:
        """Iterates over the fibre mask positions.

        Moving the ``spec`` telescope to each one of the standard star,
        acquires the new field, and adjusts the time to stay on each target.

        """

        if len(self.standards) == 0:
            return

        await self.gort.telescopes.spec.fibsel.move_to_position(self.mask_positions[0])
        await cancel_task(self.iterate_task)

        self.standards = self._get_frame()
        self.current_standard = 1

        self.standards.loc[1, "acquired"] = 1
        self.standards.loc[1, "t0"] = time()
        self.standards.loc[1, "fibre"] = self.mask_positions[0]

        self.iterate_task = asyncio.create_task(self._iterate(exposure_time))

    async def cancel(self):
        """Cancels iteration."""

        await cancel_task(self.iterate_task)

        if len(self.standards) == 0:
            return

        if self.standards.loc[self.current_standard].acquired == 1:
            if self.standards.loc[self.current_standard, "observed"] != 1:
                self.standards.loc[self.current_standard, "observed"] = 1
                self.standards.loc[self.current_standard, "t1"] = time()

    async def _iterate(self, exposure_time: float):
        """Iterate task."""

        # Time to acquire a standard.
        ACQ_PER_STD = 30

        # Tolerance to start guiding
        guide_tolerance_spec = self.gort.config["observer"]["guide_tolerance"]["spec"]

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
            self.observer.write_to_log(
                "Exposure time is too short to observe this "
                "many standards. I will do what I can.",
                "warning",
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
                self.observer.write_to_log("Exiting standard loop.")
                self.observer.write_to_log(
                    f"Standards observed: {n_observed}/{n_stds}.",
                    "info",
                )
                return

            # Time to move to another standard?
            if t_now - t0_last_std > time_per_position:
                # Check that we haven't run out standards. If so,
                # keep observing this one.
                if len(spec_coords) == current_std_idx + 1:
                    continue

                # Moving the mask to an intermediate position while we move around.
                spec_tel = self.gort.telescopes.spec
                await spec_tel.fibsel.move_relative(500)

                # Register the previous standard.
                if self.standards.loc[self.current_standard, "acquired"] == 1:
                    self.standards.loc[self.current_standard, "t1"] = time()
                    self.standards.loc[self.current_standard, "observed"] = 1

                # Increase current index and get coordinates.
                current_std_idx += 1
                self.current_standard += 1
                overhead_root = f"standards:standard-{self.current_standard}"

                # New coordinates to observe.
                new_coords = spec_coords[current_std_idx]
                new_mask_position = self.mask_positions[current_std_idx]

                # Pixel on the MF corresponding to the new fibre/mask hole on
                # which to guide. We use this tabulated list instead of
                # offset_to_master_frame_pixel() because the latter coordinates
                # are less precise as they do not include IFU rotation and more
                # precise metrology.
                new_guider_pixel = guider_pixels[new_mask_position]

                self.observer.write_to_log(
                    f"Moving to standard #{self.current_standard} ({new_coords}) "
                    f"on fibre {new_mask_position}.",
                    "info",
                )

                # Slew to new coordinates. We actually slew to the coordinates
                # that make the new star close to the fibre that will observe it.
                slew_ra, slew_dec = fibre_slew_coordinates(
                    new_coords.ra,
                    new_coords.dec,
                    new_mask_position,
                    derotated=False,
                )

                # Finish guiding on spec telescope.
                self.observer.write_to_log("Re-slewing 'spec' telescope.")

                with self.observer.register_overhead(f"{overhead_root}-slew"):
                    cotasks = [
                        self.gort.guiders["spec"].stop(),
                        spec_tel.goto_coordinates(ra=slew_ra, dec=slew_dec),
                    ]
                    await asyncio.gather(*cotasks)

                # Start to guide. Note that here we use the original coordinates
                # of the star along with the pixel on the master frame on which to
                # guide. See the note in fibre_slew_coordinates().
                self.observer.write_to_log("Starting to guide on spec telescope.")
                asyncio.create_task(
                    self.gort.guiders.spec.guide(
                        ra=new_coords.ra,
                        dec=new_coords.dec,
                        guide_tolerance=guide_tolerance_spec,
                        pixel=new_guider_pixel,
                    )
                )

                with self.observer.register_overhead(f"{overhead_root}-acquire"):
                    result = await self.gort.guiders.spec.wait_until_guiding(timeout=60)
                    if result[0] is False:
                        self.observer.write_to_log(
                            f"Timed out acquiring standard {self.current_standard}.",
                            "warning",
                        )
                        continue

                    self.observer.write_to_log(
                        f"Standard #{self.current_standard} on "
                        f"{new_mask_position!r} has been acquired."
                    )

                    # Move mask to uncover fibre.
                    await spec_tel.fibsel.move_to_position(new_mask_position)

                    # Do not guide. This means RA/Dec drifting will happen
                    # but not rotation drifting since we are guiding on a point
                    # source.
                    await self.gort.guiders.spec.apply_corrections(False)

                n_observed += 1
                t0_last_std = time()

                self.standards.loc[self.current_standard, "acquired"] = 1
                self.standards.loc[self.current_standard, "t0"] = time()
                self.standards.loc[self.current_standard, "fibre"] = new_mask_position

    def to_header(self):
        """Returns observed standards as a header-ready dictionary."""

        header_data = {}

        for nstd, data in self.standards.iterrows():
            header_data[f"STD{nstd}ID"] = data.source_id if data.source_id > 0 else None
            header_data[f"STD{nstd}RA"] = data.ra
            header_data[f"STD{nstd}DE"] = data.dec
            header_data[f"STD{nstd}ACQ"] = bool(data.observed)

            if data.observed:
                header_data[f"STD{nstd}T0"] = Time(data.t0, format="unix").isot
                header_data[f"STD{nstd}T1"] = Time(data.t1, format="unix").isot
                header_data[f"STD{nstd}EXP"] = round(data.t1 - data.t0, 1)
                header_data[f"STD{nstd}FIB"] = data.fibre

        return header_data
