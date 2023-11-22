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
import re
import warnings
from collections import defaultdict

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Sequence

from astropy.io import fits
from astropy.time import Time
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

from sdsstools.time import get_sjd

from gort.exceptions import ErrorCodes, GortSpecError
from gort.tools import (
    cancel_task,
    get_md5sum,
    get_md5sum_from_spectro,
    is_interactive,
    is_notebook,
)


if TYPE_CHECKING:
    from gort.gort import Gort, GortClient


__all__ = ["Exposure", "READOUT_TIME"]


READOUT_TIME = 51

HOOKS_TYPE = defaultdict[
    Literal["pre-readout", "post-readout"],
    list[Callable[[Any], Awaitable]],
]


class Exposure(asyncio.Future["Exposure"]):
    """A class representing an exposure taken by a :obj:`.SpectrographSet`.

    Parameters
    ----------
    gort
        A `.Gort` instance to communicate with the actors.
    exp_no
        The exposure sequence number. If :obj:`None`, the next valid
        sequence number will be used.
    flavour
        The image type. Defaults to ``'object'``.
    object
        The object name to be added to the header.
    specs
        List the spectrographs to expose. Defaults to all.

    Attributes
    ----------
    hooks
        A dictionary of hooks to call in specific steps of the exposure. Each
        hook must be a list of coroutines to call in that specific situation.
        All coroutines for a given hook are called concurrently and depending
        on the hook they may be scheduled as a task and not awaited. Available
        hooks are:
        - ``'pre-readout'`` which is called with the header before readout
          begins; the coroutine can modify the header in place or perform
          any tasks that should be complete at the end of integration.
        - ``'post-readout'`` called as a task (not awaited) after the readout
          is complete. Receives the :obj:`.Exposure` object.

        To add a coroutine to a hook ::

            async def update_header(header):
                header.update({'KEY': 1})

            exp = Exposure(g)
            exp.hooks['pre-readout'].append(update_header)

    """

    def __init__(
        self,
        gort: Gort | GortClient,
        exp_no: int | None = None,
        flavour: str | None = "object",
        object: str | None = "",
        specs: Sequence[str] | None = None,
    ):
        self.specs = gort.specs
        self.devices = specs
        self.exp_no = exp_no or self.specs.get_expno()
        self.flavour = flavour or "object"
        self.object = object or ""

        self.start_time = Time.now()
        self._exposure_time: float | None = None

        self.error: bool = False
        self.reading: bool = False

        self._timer_task: asyncio.Task | None = None
        self._progress: Progress | None = None

        self.hooks: HOOKS_TYPE = defaultdict(
            list,
            {"pre-readout": [], "post-readout": []},
        )

        if self.flavour not in ["arc", "object", "flat", "bias", "dark"]:
            raise GortSpecError(
                "Invalid flavour type.",
                error_code=ErrorCodes.USAGE_ERROR,
            )

        super().__init__()

        self.add_done_callback(self._when_done)

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
        show_progress: bool | None = None,
        object: str | None = None,
        raise_on_error: bool = True,
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
            If :obj:`None` (the default), will show the progress bar only
            in interactive sessions.
        object
            The object name to be passed to the header.
        raise_on_error
            Whether to raise an error when the exposure is marked as errored.

        """

        log = self.specs.write_to_log

        if self.specs.last_exposure is not None and not self.specs.last_exposure.done():
            log("Waiting for previous exposure to read out.", "warning")
            await self.specs.last_exposure

        # Check that all specs are idle and not errored.
        status = await self.specs.status(simple=True)
        for spec_name, spec_status in status.items():
            if self.devices is not None and spec_name not in self.devices:
                continue

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

        await self.specs.reset()

        header = header or {}

        # Set object name for header.
        if "OBJECT" not in header:
            if object is not None:
                header.update({"OBJECT": object})
            elif self.object is not None and self.object != "":
                header.update({"OBJECT": self.object})
            elif self.flavour != "object":
                header.update({"OBJECT": self.flavour})
            else:
                header.update({"OBJECT": ""})

        if show_progress is None:
            show_progress = is_interactive() or is_notebook()

        if exposure_time is None and self.flavour != "bias":
            raise GortSpecError(
                "Exposure time required for all flavours except bias.",
                error_code=ErrorCodes.USAGE_ERROR,
            )

        warnings.filterwarnings("ignore", message=".*cannot modify a done command.*")

        self.specs.last_exposure = self

        monitor_task: asyncio.Task | None = None

        self._exposure_time = exposure_time or 0.0

        log_msg = f"Taking spectrograph exposure {self.exp_no} "
        if self.flavour == "bias":
            log_msg += f"({self.flavour})."
        else:
            log_msg += f"({self.flavour or object}, {exposure_time:.1f} s)."
        log(log_msg, "info")

        try:
            if show_progress:
                await self.start_timer(self._exposure_time)

            self.start_time = Time.now()

            await self.specs.send_command_all(
                "expose",
                devices=self.devices,
                flavour=self.flavour,
                exposure_time=exposure_time,
                seqno=self.exp_no,
                readout=False,
                timeout=(exposure_time or 0) + 30,
            )

            # Call pre-readout tasks.
            await self._call_hook("pre-readout", header)

            # At this point we have integrated and are ready to read.
            self.reading = True
            readout_task = asyncio.create_task(
                self.specs.send_command_all(
                    "read",
                    devices=self.devices,
                    header=json.dumps(header),
                    timeout=90,
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
                log(f"Exposure {self.exp_no} completed.")
            else:
                await self.stop_timer()

        except Exception as err:
            # Cancel the monitor task
            await cancel_task(monitor_task)

            self.error = True
            self.set_result(self)

            if raise_on_error:
                raise GortSpecError(
                    f"Exposure failed with error {err}",
                    error_code=ErrorCodes.SECTROGRAPH_FAILED_EXPOSING,
                )
            else:
                log.warning(f"Exposure failed with error {err}")

        finally:
            await self.stop_timer()

        return self

    async def start_timer(
        self,
        exposure_time: float,
        readout_time: float = READOUT_TIME,
    ):
        """Starts the rich timer."""

        self._progress = Progress(
            TextColumn(f"[yellow]({self.exp_no})"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn("s"),
            expand=True,
            transient=True,
            auto_refresh=True,
            console=self.specs.gort._console,  # Need to use same console as logger.
        )

        exp_task = self._progress.add_task(
            "[blue] Integrating ...",
            total=int(exposure_time),
        )

        readout_task = self._progress.add_task(
            "[blue] Reading ...",
            total=int(readout_time),
            visible=False,
        )

        self._progress.start()

        async def update_timer():
            elapsed = 0
            while True:
                if elapsed > exposure_time + readout_time:
                    break
                elif self._progress is None:
                    return
                elif elapsed < exposure_time:
                    self._progress.update(exp_task, advance=1)
                else:
                    self._progress.update(
                        exp_task,
                        description="[green] Integration complete",
                        completed=int(exposure_time),
                    )
                    self._progress.update(readout_task, advance=1, visible=True)

                await asyncio.sleep(1)
                elapsed += 1

            if self._progress and readout_task:
                self._progress.update(
                    readout_task,
                    completed=int(readout_time),
                    description="[green] Readout complete",
                )

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

        config = self.specs.gort.config["specs"]

        HEADERS_CRITICAL = config["verification"]["headers"]["critical"]
        HEADERS_WARNING = config["verification"]["headers"]["warning"]

        files = self.get_files()

        n_spec = len(self.devices) if self.devices else len(config["devices"])
        n_files_expected = n_spec * 3

        if (n_files_found := len(files)) < n_files_expected:
            raise RuntimeError(
                f"Expected {n_files_expected} files but found {n_files_found}. "
                "Verification failed."
            )

        for file in files:
            header = fits.getheader(str(file))

            for key in HEADERS_CRITICAL:
                if key not in header:
                    raise RuntimeError(f"Keyword {key} not present in {file!s}")

            for key in HEADERS_WARNING:
                if key not in header:
                    self.specs.write_to_log(
                        f"Keyword {key} not present in {file!s}",
                        "warning",
                    )

            md5sum_spectro = get_md5sum_from_spectro(file)
            md5sum = get_md5sum(file)
            if md5sum != md5sum_spectro:
                raise RuntimeError(f"MD5 checksum validation failed for file {file!r}")

        if len(files) > 0:
            pattern_path = re.sub("([rbz][1-3])", "*", str(files[0]))
            self.specs.write_to_log(f"Files saved to {pattern_path!r}.")

    def get_files(self):
        """Returns the files written by the exposure."""

        sjd = get_sjd("LCO")
        config = self.specs.gort.config
        data_path = pathlib.Path(config["specs"]["data_path"].format(SJD=sjd))

        return list(data_path.glob(f"*-[0]*{self.exp_no}.fits.gz"))

    def _when_done(self, result):
        """Called when the future is done."""

        asyncio.create_task(self._call_hook("post-readout", self, as_task=True))

    async def _done_monitor(self):
        """Waits until the spectrographs are idle, and marks the Future done."""

        await self.specs.send_command_all("wait_until_idle", allow_errored=True)

        self.reading = False

        for spec in self.specs.values():
            reply = await spec.status(simple=True)
            if "ERROR" in reply["status_names"]:
                self.error = True

        self.verify_files()

        # Set the Future.
        self.set_result(self)

    async def _call_hook(self, hook_name: str, *args, as_task: bool = False, **kwargs):
        """Calls the coroutines associated with a hook."""

        if hook_name not in self.hooks:
            raise ValueError(f"Invalid hook {hook_name!r}.")

        coros = self.hooks[hook_name]
        if not isinstance(coros, list) and asyncio.iscoroutinefunction(coros):
            task = asyncio.create_task(coros(*args, **kwargs))
        elif isinstance(coros, list):
            task = asyncio.gather(*[coro(*args, **kwargs) for coro in coros])
        else:
            raise ValueError(f"Invalid hook functions found for {hook_name!r}.")

        if not as_task:
            await task
