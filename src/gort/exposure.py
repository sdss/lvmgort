#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-08-11
# @Filename: exposure.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import json
import pathlib
import warnings

from typing import TYPE_CHECKING, Any

import pandas
from astropy.io import fits
from astropy.time import Time
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

from sdsstools.time import get_sjd

from gort.exceptions import ErrorCodes, GortSpecError
from gort.tools import build_guider_reply_list, cancel_task, register_observation


if TYPE_CHECKING:
    from gort.devices import SpectrographSet


__all__ = ["Exposure", "READOUT_TIME"]


READOUT_TIME = 51


class Exposure(asyncio.Future["Exposure"]):
    """A class representing an exposure taken by a :obj:`.SpectrographSet`.

    Parameters
    ----------
    exp_no
        The exposure sequence number.
    spec_set
        The :obj:`.SpectrographSet` commanding this exposure.
    flavour
        The image type.

    """

    def __init__(self, exp_no: int, spec_set: SpectrographSet, flavour: str = "object"):
        self.spec_set = spec_set
        self.exp_no = exp_no
        self.flavour = flavour
        self.object: str = ""
        self.start_time = Time.now()

        self.error: bool = False
        self.reading: bool = False

        self._timer_task: asyncio.Task | None = None
        self._progress: Progress | None = None

        self._guider_task: asyncio.Task | None = None
        self.guider_data: pandas.DataFrame | None = None

        super().__init__()

    def __repr__(self):
        return (
            f"<Exposure (exp_no={self.exp_no}, flavour={self.flavour}, "
            f"object={self.object!r} error={self.error}, reading={self.reading}, "
            f"done={self.done()})>"
        )

    async def expose(
        self,
        exposure_time: float | None = None,
        header: dict | None = None,
        async_readout: bool = False,
        show_progress: bool = False,
        **kwargs,
    ):
        """Exposes the spectrograph.

        Parameters
        ----------
        exposure_time
            The exposure time.
        header
            A dictionary with the extra header values..
        async_readout
            Returns after integration completes. Readout is initiated
            but handled asynchronously and can be await by awaiting
            the returned :obj:`.Exposure` object.
        show_progress
            Displays a progress bar with the elapsed exposure time.
        kwargs
            Keyword arguments to pass to ``lvmscp expose``.

        """

        # Check that all specs are idle and not errored.
        status = await self.spec_set.status(simple=True)
        for spec_status in status.values():
            if "IDLE" not in spec_status["status_names"]:
                raise GortSpecError(
                    "Some spectrographs are not IDLE.",
                    error_code=ErrorCodes.SECTROGRAPH_NOT_IDLE,
                )
            if "ERROR" in spec_status["status_names"]:
                raise GortSpecError(
                    "Some spectrographs have ERROR status. "
                    "Solve this manually before exposing.",
                    error_code=ErrorCodes.SECTROGRAPH_NOT_IDLE,
                )

        if show_progress:
            await self.start_timer(exposure_time or 0.0)

        if (
            exposure_time is None
            and kwargs.get("flavour", "object") != "bias"
            and not kwargs.get("bias", False)
        ):
            raise GortSpecError(
                "Exposure time required for all flavours except bias.",
                error_code=ErrorCodes.USAGE_ERROR,
            )

        warnings.filterwarnings("ignore", message=".*cannot modify a done command.*")

        self.spec_set.last_exposure = self

        monitor_task: asyncio.Task | None = None

        if self.flavour == "object":
            guider_task = asyncio.create_task(self._guider_monitor())
        else:
            guider_task = None

        try:
            self.start_time = Time.now()

            await self.spec_set._send_command_all(
                "expose",
                exposure_time=exposure_time,
                seqno=self.exp_no,
                readout=False,
                **kwargs,
            )

            # At this point we have integrated and are ready to read.

            self.reading = True

            await cancel_task(guider_task)

            header = header or {}
            await self._update_header(header)

            readout_task = asyncio.create_task(
                self.spec_set._send_command_all(
                    "read",
                    header=json.dumps(header),
                )
            )

            # Now launch the task that marks the Future done when the spec
            # is IDLE. If async_readout=False then that will return immediately
            # because the spec is already idle. If async_readout=True, this method
            # will return now and the task will mark the Future done when readout
            # complete (readout is ongoing and does not need to be launched).
            monitor_task = asyncio.create_task(self._done_monitor())
            if not async_readout:
                await readout_task
                await monitor_task
            else:
                await self.stop_timer()
                self.spec_set.write_to_log("Returning with async readout ongoing.")

        except Exception as err:
            # Cancel the monitor task
            await cancel_task(monitor_task)

            self.error = True
            self.set_result(self)

            raise GortSpecError(f"Exposure failed with error {err}", error_code=301)

        finally:
            await self.stop_timer()

            if self.done() and not self.error:
                self.verify_files()

        return self

    async def _guider_monitor(self):
        """Monitors the guider data and build a data frame."""

        current_data = []

        task = asyncio.create_task(
            build_guider_reply_list(
                self.spec_set.gort,
                current_data,
            )
        )

        try:
            while True:
                # Just keep the task running.
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            await cancel_task(task)

            if len(current_data) > 0:
                # Build DF with all the frames.
                df = pandas.DataFrame.from_records(current_data)

                # Group by frameno, keep only non-NaN values.
                df = df.groupby(["frameno", "telescope"], as_index=False).apply(
                    lambda g: g.fillna(method="bfill", axis=0).iloc[0, :]
                )

                # Remove NaN rows.
                df = df.dropna()

                # Sort by frameno.
                df = df.sort_values("frameno")

                self.guider_data = df

            return

    async def start_timer(
        self,
        exposure_time: float,
        readout_time: float = READOUT_TIME,
    ):
        """Starts the tqdm timer."""

        self._progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn("s"),
            expand=True,
            transient=True,
            auto_refresh=True,
            console=self.spec_set.gort._console,  # Need to use same console as logger.
        )

        exp_task = self._progress.add_task(
            "[green]Integrating ...",
            total=int(exposure_time),
        )

        self._progress.start()

        async def update_timer():
            readout_task = None
            elapsed = 0
            while True:
                if elapsed > exposure_time + readout_time:
                    break
                elif self._progress is None:
                    return
                elif elapsed < exposure_time:
                    self._progress.update(exp_task, advance=1)
                else:
                    if readout_task is None:
                        readout_task = self._progress.add_task(
                            "[red]Reading ...",
                            total=int(readout_time),
                        )
                    self._progress.update(exp_task, completed=int(exposure_time))
                    self._progress.update(readout_task, advance=1)

                await asyncio.sleep(1)
                elapsed += 1

            if self._progress and readout_task:
                self._progress.update(readout_task, completed=int(readout_time))

        def done_timer(*_):
            if self._progress:
                self._progress.stop()
                self._progress.console.clear_live()
                self._progress = None

        self._timer_task = asyncio.create_task(update_timer())
        self._timer_task.add_done_callback(done_timer)

        return

    async def stop_timer(self):
        """Cancels the timer."""

        await cancel_task(self._timer_task)
        self._timer_task = None

        if self._progress:
            self._progress.stop()
            self._progress.console.clear_live()
        self._progress = None

    def verify_files(self):
        """Checks that the files have been written and have the right contents."""

        HEADERS_CRITICAL = [
            "TILE_ID",
            "DPOS",
            "ARGON",
            "NEON",
            "LDLS",
            "QUARTZ",
            "HGNE",
            "XENON",
            "HARTMANN",
            "TESCIRA",
            "TESCIDE",
            "TESKYERA",
            "TESKYEDE",
            "TESKYWRA",
            "TESKYWDE",
            "TESPECRA",
            "TESPECDE",
        ]

        HEADERS_WARNING = []

        for file in self.get_files():
            header = fits.getheader(str(file))

            for key in HEADERS_CRITICAL:
                if key not in header:
                    raise GortSpecError(f"Keyword {key} not present in {file!s}")

            for key in HEADERS_WARNING:
                if key not in header:
                    self.spec_set.write_to_log(
                        f"Keyword {key} not present in {file!s}",
                        "warning",
                    )

    def get_files(self):
        """Returns the files written by the exposure."""

        sjd = get_sjd("LCO")
        config = self.spec_set.gort.config
        data_path = pathlib.Path(config["specs"]["data_path"].format(SJD=sjd))

        return list(data_path.glob(f"*-[0]*{self.exp_no}.fits.gz"))

    async def _done_monitor(self):
        """Waits until the spectrographs are idle, and marks the Future done."""

        await self.spec_set._send_command_all("wait_until_idle", allow_errored=True)

        for spec in self.spec_set.values():
            reply = await spec.status(simple=True)
            if "ERROR" in reply["status_names"]:
                self.error = True

        self.reading = False

        # Set the Future.
        self.set_result(self)

    async def register_observation(
        self,
        tile_id: int | None = None,
        dither_pos: int = 0,
    ):
        """Registers the exposure in the database."""

        if self.flavour != "object":
            return

        if self.guider_data is not None and len(self.guider_data.dropna()) > 0:
            seeing = self.guider_data.dropna().fwhm.mean()
        else:
            seeing = -999

        self.spec_set.write_to_log("Registering observation.", "info")
        registration_payload = {
            "dither": dither_pos,
            "jd": self.start_time.jd,
            "seeing": seeing,
            "standards": [],
            "skies": [],
            "exposure_no": self.exp_no,
        }

        if tile_id is not None:
            registration_payload["tile_id"] = tile_id

        self.spec_set.write_to_log(f"Registration payload {registration_payload}")

        try:
            await register_observation(registration_payload)
        except Exception as err:
            self.spec_set.write_to_log(f"Failed registering exposure: {err}", "error")
        else:
            self.spec_set.write_to_log("Registration complete.")

    async def _update_header(self, header: dict[str, Any]):
        """Updates the exposure header with pointing and guiding information."""

        return
