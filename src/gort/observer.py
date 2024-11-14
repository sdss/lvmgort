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
import signal
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial, wraps
from time import time

from typing import TYPE_CHECKING, Any, Callable, TypedDict

from astropy.time import Time

from sdsstools.utils import GatheringTaskGroup

from gort.enums import Event, GuiderStatus, ObserverStageStatus
from gort.exceptions import (
    ErrorCode,
    GortError,
    GortObserverCancelledError,
    GortObserverError,
)
from gort.exposure import Exposure
from gort.tile import Coordinates, Tile
from gort.tools import (
    GuiderMonitor,
    cancel_task,
    handle_signals,
    insert_to_database,
    register_observation,
    run_in_executor,
)
from gort.transforms import fibre_slew_coordinates, wrap_pa_hex


if TYPE_CHECKING:
    from gort.gort import Gort


__all__ = ["GortObserver"]


class InterrupHandlerHelper:
    """Helper for handling interrupts"""

    def __init__(self):
        self.observer: GortObserver | None = None
        self._callback: Callable | None = None

    def run_callback(self):
        if self._callback is None:
            return

        if self.observer:
            if (exposure := self.observer._current_exposure) is not None:
                exposure.error = True
                exposure.set_result(exposure)
                exposure.stop_timer()

            self.observer.gort.log.warning("Running cleanup due to keyboard interrupt.")

        self._callback()

    def set_callback(self, cb: Callable | None):
        self._callback = cb


interrupt_helper = InterrupHandlerHelper()
interrupt_signals = [signal.SIGINT, signal.SIGTERM]


class StagesDict(TypedDict):
    """A dictionary of observer stages."""

    slew: ObserverStageStatus
    acquire: ObserverStageStatus
    expose: ObserverStageStatus


def register_stage_status(coro):
    """Records the status of the stage."""

    @wraps(coro)
    async def wrapper(*args, **kwargs):
        self: GortObserver = args[0]
        stage = coro.__name__

        tile = self._tile
        tile_id = tile.tile_id if tile else None
        dither_position = self.dither_position if tile else None

        payload = {
            "tile_id": tile_id,
            "dither_position": dither_position,
            "stage": stage,
        }

        if self.cancelling:
            raise GortObserverCancelledError()

        self.stages[stage] = ObserverStageStatus.RUNNING
        await self.gort.notify_event(Event.OBSERVER_STAGE_RUNNING, payload=payload)

        try:
            result = await coro(*args, **kwargs)
        except Exception:
            self.stages[stage] = ObserverStageStatus.FAILED
            await self.gort.notify_event(Event.OBSERVER_STAGE_FAILED, payload=payload)
            raise

        self.stages[stage] = ObserverStageStatus.DONE
        await self.gort.notify_event(Event.OBSERVER_STAGE_DONE, payload=payload)

        return result

    return wrapper


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
    on_interrupt
        Callback to be called when the observation is interrupted.

    """

    def __init__(
        self,
        gort: Gort,
        tile: Tile | None = None,
        mask_positions_pattern: str = "P1-*",
        on_interrupt: Callable | None = None,
    ):
        self.gort = gort
        self._tile = tile

        self.mask_positions = self._get_mask_positions(mask_positions_pattern)

        self.guide_task: asyncio.Future | None = None
        self.guider_monitor = GuiderMonitor(self.gort)

        self.standards: Standards | None = None

        self.dither_position: int = 0
        self._current_exposure: Exposure | None = None

        self.overheads: dict[str, dict[str, int | float]] = {}

        self.stages: StagesDict = {
            "slew": ObserverStageStatus.WAITING,
            "acquire": ObserverStageStatus.WAITING,
            "expose": ObserverStageStatus.WAITING,
        }

        self.cancelling: bool = False

        interrupt_helper.set_callback(on_interrupt)
        interrupt_helper.observer = self

        # Necessary because this sets the standards
        # and dither position if the tile is not null.
        self.reset(tile)

    def reset(
        self,
        tile: Tile | None = None,
        on_interrupt: Callable | None = None,
        reset_stages: bool = True,
    ):
        """Resets the observer."""

        self._tile = tile
        self.standards = Standards(self, tile) if tile else None
        self.dither_position = tile.sci_coords.dither_position if tile else 0

        self.guide_task = None
        self.guider_monitor.reset()

        self._current_exposure = None
        self.overheads = {}

        self.cancelling = False

        if reset_stages:
            self.stages: StagesDict = {
                "slew": ObserverStageStatus.WAITING,
                "acquire": ObserverStageStatus.WAITING,
                "expose": ObserverStageStatus.WAITING,
            }

        if on_interrupt is not None:
            interrupt_helper.set_callback(on_interrupt)

    @property
    def tile(self):
        """Returns the current tile being observed."""

        if not self._tile:
            raise GortObserverError("No tile has been set.")

        return self._tile

    def set_tile(self, tile: Tile):
        """Sets the current tile. Implies reset."""

        self.reset(tile)

    def is_running(self):
        """Returns :obj:`True` if the observer is running."""

        return self.get_running_stage() is not None

    def get_running_stage(self):
        """Returns the running stage."""

        for stage, status in self.stages.items():
            if status == ObserverStageStatus.RUNNING:
                return stage

        return None

    def __repr__(self):
        tile_id = self._tile.tile_id if self._tile else "none"
        return f"<GortObserver (tile_id={tile_id})>"

    @property
    def has_standards(self):
        """Returns :obj:`True` if standards will be observed."""

        if not self.standards:
            return False

        return len(self.standards.standards) > 0

    async def observe_tile(
        self,
        tile: Tile | int | None = None,
        ra: float | None = None,
        dec: float | None = None,
        pa: float = 0.0,
        use_scheduler: bool = True,
        dither_position: int | None = None,
        exposure_time: float = 900.0,
        n_exposures: int = 1,
        async_readout: bool = False,
        keep_guiding: bool = False,
        skip_slew_when_acquired: bool = True,
        guide_tolerance: float = 1.0,
        acquisition_timeout: float = 180.0,
        show_progress: bool | None = None,
        run_cleanup: bool = True,
        adjust_focus: bool = True,
        cleanup_on_interrupt: bool = True,
    ) -> tuple[bool, list[Exposure]]:
        """Performs all the operations necessary to observe a tile.

        Parameters
        ----------
        tile
            The ``tile_id`` to observe, or a :obj:`.Tile` object. If not
            provided, observes the next tile suggested by the scheduler
            (requires ``use_scheduler=True``).
        ra,dec
            The RA and Dec where to point the science telescopes. The other
            telescopes are pointed to calibrators that fit the science pointing.
            Cannot be used with ``tile``.
        pa
            Position angle of the IFU. Defaults to PA=0.
        use_scheduler
            Whether to use the scheduler to determine the ``tile_id`` or
            select calibrators.
        dither_position
            The dither position to use. If not provided, uses the tile default
            dither position or zero.
        exposure_time
            The length of the exposure in seconds.
        n_exposures
            Number of exposures to take while guiding.
        async_readout
            Whether to wait for the readout to complete or return as soon
            as the readout begins. If :obj:`False`, the exposure is registered
            but the observation is not finished. This should be :obj:`True`
            during normal science operations to allow the following acquisition
            to occur during readout.
        keep_guiding
            If :obj:`True`, keeps the guider running after the last exposure.
            This should be :obj:`False` during normal science operations.
        skip_slew_when_acquired
            If the tile has been acquired and is guiding, skips the slew,
            modifies the dither position, waits until the new position has
            been acquired, and starts exposing.
        guide_tolerance
            The guide tolerance in arcsec. A telescope will not be considered
            to be guiding if its separation to the commanded field is larger
            than this value.
        acquisition_timeout
            The maximum time allowed for acquisition. In case of timeout
            the acquired fields are evaluated and an exception is
            raised if the acquisition failed.
        show_progress
            Displays a progress bar with the elapsed exposure time.
        run_cleanup
            Whether to run the cleanup routine.
        adjust_focus
            Adjusts the focuser positions based on temperature drift before
            starting the observation. This works best if the focus has been
            initially determined using a focus sweep.
        cleanup_on_interrupt
            If ``True``, registers a signal handler to catch interrupts and
            run the cleanup routine.

        """

        write_log = self.write_to_log

        # Create tile.
        if isinstance(tile, Tile):
            pass
        elif tile is not None or (tile is None and ra is None and dec is None):
            if use_scheduler:
                tile = await run_in_executor(Tile.from_scheduler, tile_id=tile)
            else:
                raise GortError("Not enough information to create a tile.")

        elif ra is not None and dec is not None:
            if use_scheduler:
                tile = await run_in_executor(Tile.from_scheduler, ra=ra, dec=dec, pa=pa)
            else:
                tile = await run_in_executor(Tile.from_coordinates, ra, dec, pa=pa)

        else:
            raise GortError("Not enough information to create a tile.")

        assert isinstance(tile, Tile)

        if dither_position is not None:
            tile.set_dither_position(dither_position)
        elif tile.sci_coords.dither_position is None:
            self.write_to_log(
                "No dither position defined. Using dither_position=0.",
                "warning",
            )
            tile.set_dither_position(0)

        if cleanup_on_interrupt:
            interrupt_cb = partial(self.gort.run_script_sync, "cleanup")
        else:
            interrupt_cb = None

        is_acquired: bool = False
        is_guiding = self.gort.guiders.sci.status & GuiderStatus.GUIDING

        # We require the tile to be acquired and guiding to skip the slew. The current
        # tile must match the tile_id of the new tile requested and acquisition must
        # have been completed.
        if (
            skip_slew_when_acquired
            and self._tile is not None
            and tile.tile_id
            and self._tile.tile_id == tile.tile_id
            and is_guiding
            and self.stages["acquire"] == ObserverStageStatus.DONE
        ):
            is_acquired = True

        # Reset the tile
        self.reset(tile, on_interrupt=interrupt_cb, reset_stages=not is_acquired)

        # Run the cleanup routine to be extra sure.
        if run_cleanup:
            await self.gort.cleanup(turn_lamps_off=False)

        # Wrap the PA to the range -30 to 30.
        pa = tile.sci_coords.pa
        new_pa = wrap_pa_hex(tile.sci_coords.pa)
        if new_pa != pa:
            write_log(f"Wrapping sci PA from {pa:.3f} to {new_pa:.3f}.")
            tile.sci_coords.pa = new_pa

        if tile.tile_id is not None:
            write_log(
                f"Observing tile_id={tile.tile_id} on "
                f"dither position #{self.dither_position}.",
                "info",
            )

        if adjust_focus:
            await self.gort.guiders.adjust_focus()

        await self.gort.notify_event(
            Event.OBSERVER_NEW_TILE,
            payload={"tile_id": tile.tile_id, "dither_position": self.dither_position},
        )

        exposures: list[Exposure] = []
        failed: bool = False

        try:
            if not is_acquired:
                # Slew telescopes and move fibsel mask.
                await self.slew()

                # Start guiding.
                await self.acquire(
                    guide_tolerance=guide_tolerance,
                    timeout=acquisition_timeout,
                )

            else:
                write_log(f"Acquiring dither position #{self.dither_position}", "info")

                async with GatheringTaskGroup() as group:
                    group.create_task(self.set_dither_position(self.dither_position))
                    if self.standards:
                        group.create_task(self.standards.reacquire_first())

                # Need to restart the guider monitor so that the new exposure
                # gets the range of guider frames that correspond to this dither.
                # GortObserver.expose() doesn't do this because we ask for a single
                # exposure.
                self.guider_monitor.reset()

            # Exposing
            _exposure = await self.expose(
                exposure_time=exposure_time,
                show_progress=show_progress,
                count=n_exposures,
                async_readout=async_readout,
                keep_guiding=keep_guiding,
                dither_position=self.dither_position,
            )

            if not isinstance(_exposure, list):
                exposures = [_exposure]
            else:
                exposures = _exposure

            if self.cancelling:
                await self.gort.guiders.stop()

                write_log("Reading exposure before cancelling.", "warning")
                await asyncio.gather(*exposures)

                raise GortObserverCancelledError()

        except GortObserverCancelledError:
            write_log("Observation cancelled.", "warning")
            failed = True

        except KeyboardInterrupt:
            write_log("Observation interrupted by user.", "warning")
            failed = True

        finally:
            # Finish observation.
            await self.finish_observation(keep_guiding=keep_guiding and not failed)

        return (not failed, exposures)

    @handle_signals(interrupt_signals, interrupt_helper.run_callback)
    @register_stage_status
    async def slew(self):
        """Slew to the telescope fields."""

        cotasks = []

        with self.register_overhead("slew:stop-guiders"):
            # Stops guiders.
            await self.gort.guiders.stop()

        # Slew telescopes.
        self.write_to_log(f"Slewing to tile_id={self.tile.tile_id}.", level="info")

        sci = (
            self.tile.sci_coords.ra,
            self.tile.sci_coords.dec,
            self.tile.sci_coords.pa,
        )
        self.write_to_log(f"Science: {str(self.tile.sci_coords)}")

        spec = None
        if self.tile.spec_coords and len(self.tile.spec_coords) > 0:
            first_spec = self.tile.spec_coords[0]

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
            if skytel.lower() in self.tile.sky_coords:
                sky_coords_tel = self.tile.sky_coords[skytel.lower()]
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
        cotasks.append(fibsel.move_to_position(self.mask_positions[0], rehome=True))

        # Execute.
        with self.register_overhead("slew:slew"):
            await asyncio.gather(*cotasks)

    @handle_signals(interrupt_signals, interrupt_helper.run_callback)
    @register_stage_status
    async def acquire(
        self,
        guide_tolerance: float | None = None,
        timeout: float = 180,
        telescopes: list[str] = ["sci", "skye", "skyw", "spec"],
    ):
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
        telescopes
            The list of telescopes to acquire. By default all telescopes are
            acquired.

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
        for tel in telescopes:
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
            self.guider_monitor.start_monitoring()
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
                        error_code=ErrorCode.ACQUISITION_FAILED,
                        payload={
                            "observer": True,
                            "tile_id": self.tile.tile_id,
                            "telescope": "sci",
                        },
                    )

                if tel == "spec":
                    if not is_guiding:
                        raise GortObserverError(
                            "Spec telescope is not guiding.",
                            error_code=ErrorCode.ACQUISITION_FAILED,
                            payload={
                                "observer": True,
                                "tile_id": self.tile.tile_id,
                                "telescope": "spec",
                                "fibsel_position": self.mask_positions[0],
                            },
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

    @handle_signals(interrupt_signals, interrupt_helper.run_callback)
    @register_stage_status
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

        last_exposure = self.gort.specs.last_exposure
        if last_exposure is not None and not last_exposure.done():
            self.write_to_log("Waiting for previous exposure to read out.", "warning")
            await last_exposure

        # Last chance to bail out before the exposure.
        if self.cancelling:
            raise GortObserverCancelledError()

        exposures: list[Exposure] = []
        _n_exposures = 0

        for nexp in range(1, count + 1):
            self.write_to_log(
                f"Starting {exposure_time:.1f} s exposure ({nexp}/{count}).",
                "info",
            )

            with self.register_overhead(f"expose:pre-exposure-{nexp}"):
                # Refresh guider data for this exposure.
                if _n_exposures > 0:
                    self.guider_monitor.reset()

                # Move fibre selector to the first position. Should be there unless
                # count > 1 in which case we need to move it back.
                if self.standards:
                    await self.standards.start_iterating(exposure_time)

                exposure = Exposure(self.gort, flavour="object", object=object)
                self._current_exposure = exposure

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
            _n_exposures += 1

        if len(exposures) == 1:
            return exposures[0]
        else:
            return exposures

    @handle_signals(interrupt_signals, interrupt_helper.run_callback)
    async def finish_observation(self, keep_guiding: bool = False):
        """Finishes the observation, stops the guiders, etc."""

        self.write_to_log("Finishing observation.", "info")

        with self.register_overhead("finish-observation"):
            # Should have been cancelled in _update_header(), but just in case.
            if self.standards is not None:
                await self.standards.cancel()

            if (
                not keep_guiding
                and self.guide_task is not None
                and not self.guide_task.done()
            ):
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
                "dither_position": value["dither_position"],
                "stage": name,
                "start_time": value["t0"],
                "end_time": value["t0"] + value["elapsed"],
                "duration": value["elapsed"],
            }
            for name, value in self.overheads.items()
        ]

        try:
            table_name = self.gort.config["services.database.tables.overheads"]
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
        if self.standards:
            for stdn, std_data in self.standards.standards.items():
                if std_data.observed == 1:
                    pk = self.tile.spec_coords[stdn - 1].pk
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
        event: Event | None = None,
        extra_payload: dict[str, Any] = {},
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
        event
            If specified, emits an event of this type.
        extra_payload
            Additional payload to include in the event.

        """

        if header is None:
            header = f"({self.__class__.__name__}) "

        message = f"{header}{message}"

        level_int = logging._nameToLevel[level.upper()]
        self.gort.log.log(level_int, message)

        if event:
            payload = {
                "observer": True,
                "message": message,
                "level": level,
                "tile_id": self.tile.tile_id,
            }
            payload.update(extra_payload)

            asyncio.create_task(self.gort.notify_event(event, payload=payload))

    @contextmanager
    def register_overhead(self, name: str):
        """Measures and registers and overhead."""

        t0 = time()

        yield

        self.overheads[name] = {
            "dither_position": self.tile.sci_coords.dither_position,
            "t0": t0,
            "elapsed": time() - t0,
        }

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

        header.update(self.guider_monitor.to_header())

        # At this point the shutter is closed so let's stop observing standards.
        # This also finishes updating the standards table.
        if self.has_standards and self.standards:
            await self.standards.cancel()

            # There is some overhead between when we set t0 for the first standard
            # and when the exposure actually begins. This leads to the first standard
            # having longer exposure time than open shutter.
            if self.has_standards and self._current_exposure is not None:
                start_time = self._current_exposure.start_time.unix
                self.standards.standards[1].t0 = start_time

            header.update(self.standards.to_header())

    async def _post_readout(self, exposure: Exposure):
        """Post exposure tasks."""

        # tile_id = self.tile.tile_id
        # dither_pos = self.tile.sci_coords.dither_position

        # if exposure.error is True:
        #     if tile_id is not None:
        #         mark_exposure_bad(tile_id, dither_pos)

        #     self.write_to_log(
        #         "Exposure returned with errors. Tile-dither has been marked as bad.",
        #         "error",
        #     )

        return


@dataclass
class Standard:
    """A class to represent a standard star."""

    n: int
    ra: float
    dec: float
    source_id: int = -1
    acquired: bool = False
    observed: bool = False
    t0: float = 0.0
    t1: float = 0.0
    fibre: str = ""


class Standards:
    """Iterates over standards and monitors observed standards."""

    def __init__(
        self,
        observer: GortObserver,
        tile: Tile,
    ):
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

        standards: dict[int, Standard] = {}

        for stdn, cc in enumerate(self.tile.spec_coords):
            standards[stdn + 1] = Standard(
                n=stdn + 1,
                ra=cc.ra,
                dec=cc.dec,
                source_id=cc.source_id or -1,
            )

        return standards

    async def start_iterating(self, exposure_time: float) -> None:
        """Iterates over the fibre mask positions.

        Moving the ``spec`` telescope to each one of the standard star,
        acquires the new field, and adjusts the time to stay on each target.

        # TODO: right now we are assuming that the first standard has been
        # acquired when we call this method. This is not always the case and
        # it caused a bug when observing multiple dither positions so we should
        # check.

        """

        if len(self.standards) == 0:
            return

        await self.gort.telescopes.spec.fibsel.move_to_position(self.mask_positions[0])
        await cancel_task(self.iterate_task)

        self.standards = self._get_frame()
        self.current_standard = 1

        self.standards[1].acquired = True
        self.standards[1].t0 = time()
        self.standards[1].fibre = self.mask_positions[0]

        self.iterate_task = asyncio.create_task(self._iterate(exposure_time))

    async def reacquire_first(self):
        """Re-acquires the first standard.

        This method should only be called when observing second and subsequent
        dithers in a tile.

        """

        if self.iterate_task and not self.iterate_task.done():
            await self.cancel()

        self.current_standard = 1

        # Home the fibsel just to be sure.
        await self.gort.telescopes.spec.fibsel.home()

        if not (await self.acquire_standard(0)):
            raise GortObserverError("Failed to re-acquire first standard.")

    async def acquire_standard(self, standard_idx: int):
        """Acquires a standard star and starts guiding on it."""

        guider_pixels: dict[str, tuple[float, float]]
        guider_pixels = self.gort.config["guiders"]["devices"]["spec"]["named_pixels"]

        # Tolerance to start guiding
        guide_tolerance_spec = self.gort.config["observer"]["guide_tolerance"]["spec"]

        # Moving the mask to an intermediate position while we move around.
        spec_tel = self.gort.telescopes.spec
        await spec_tel.fibsel.move_relative(500)

        overhead_root = f"standards:standard-{self.current_standard}"

        # New coordinates to observe.
        new_coords = self.tile.spec_coords[standard_idx]
        new_mask_position = self.mask_positions[standard_idx]

        event_payload = {
            "observer": True,
            "tile_id": self.tile.tile_id,
            "telescope": "spec",
            "n_standard": self.current_standard,
            "fibsel_position": new_mask_position,
            "coordinates": [new_coords.ra, new_coords.dec],
        }

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

        await self.gort.notify_event(
            Event.OBSERVER_ACQUISITION_START,
            payload=event_payload,
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
                spec_tel.goto_coordinates(ra=slew_ra, dec=slew_dec, force=True),
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
                    event=Event.OBSERVER_STANDARD_ACQUISITION_FAILED,
                    extra_payload={**event_payload, "reason": "timeout"},
                )
                return False

            self.observer.write_to_log(
                f"Standard #{self.current_standard} on "
                f"{new_mask_position!r} has been acquired.",
                "info",
            )

            # Move mask to uncover fibre.
            await spec_tel.fibsel.move_to_position(new_mask_position)

            # Do not guide. This means RA/Dec drifting will happen
            # but not rotation drifting since we are guiding on a point
            # source.
            await self.gort.guiders.spec.apply_corrections(False)

        return True

    async def cancel(self):
        """Cancels iteration."""

        self.observer.write_to_log("Cancelling standards iteration.", "debug")
        await cancel_task(self.iterate_task)

        if len(self.standards) == 0:
            return

        if self.standards[self.current_standard].acquired:
            if not self.standards[self.current_standard].observed:
                self.standards[self.current_standard].observed = True
                self.standards[self.current_standard].t1 = time()

    async def _iterate(self, exposure_time: float):
        """Iterate task."""

        # Time to acquire a standard.
        ACQ_PER_STD = 30

        spec_coords = self.tile.spec_coords

        # If we have zero or one standards, do nothing. The spec telescope
        # is already pointing to the first mask position.
        if len(spec_coords) <= 1:
            return

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

                # Register the previous standard.
                if self.standards[self.current_standard].acquired:
                    self.standards[self.current_standard].t1 = time()
                    self.standards[self.current_standard].observed = True

                # Increase current index and acquire the next standard.
                current_std_idx += 1
                self.current_standard += 1

                if not (await self.acquire_standard(current_std_idx)):
                    continue

                n_observed += 1
                t0_last_std = time()

                new_mask_position = self.mask_positions[current_std_idx]

                self.standards[self.current_standard].acquired = True
                self.standards[self.current_standard].t0 = time()
                self.standards[self.current_standard].fibre = new_mask_position

    def to_header(self):
        """Returns observed standards as a header-ready dictionary."""

        header_data = {}

        for nstd, data in self.standards.items():
            header_data[f"STD{nstd}ID"] = data.source_id if data.source_id > 0 else None
            header_data[f"STD{nstd}RA"] = data.ra
            header_data[f"STD{nstd}DE"] = data.dec
            header_data[f"STD{nstd}ACQ"] = data.observed

            if data.observed:
                header_data[f"STD{nstd}T0"] = Time(data.t0, format="unix").isot
                header_data[f"STD{nstd}T1"] = Time(data.t1, format="unix").isot
                header_data[f"STD{nstd}EXP"] = round(data.t1 - data.t0, 1)
                header_data[f"STD{nstd}FIB"] = data.fibre

        return header_data
