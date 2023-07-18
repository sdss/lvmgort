#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-15
# @Filename: spec.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import json
import pathlib
import warnings
from contextlib import suppress
from copy import deepcopy

from typing import TYPE_CHECKING, Any

import jsonschema
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

from sdsstools.time import get_sjd

from gort import config
from gort.exceptions import ErrorCodes, GortSpecError
from gort.gort import GortDevice, GortDeviceSet
from gort.tools import is_interactive, is_notebook, move_mask_interval


if TYPE_CHECKING:
    from gort.core import ActorReply
    from gort.gort import GortClient


__all__ = ["Spectrograph", "SpectrographSet", "Exposure", "IEB", "READOUT_TIME"]


READOUT_TIME = 51


class Exposure(asyncio.Future["Exposure"]):
    """A class representing an exposure taken by a `.SpectrographSet`.

    Parameters
    ----------
    exp_no
        The exposure sequence number.
    spec_set
        The `.SpectrographSet` commanding this exposure.

    """

    def __init__(self, exp_no: int, spec_set: SpectrographSet):
        self.spec_set = spec_set
        self.exp_no = exp_no

        self.error: bool = False
        self.reading: bool = False

        self._timer_task: asyncio.Task | None = None
        self._progress: Progress | None = None

        super().__init__()

    def __repr__(self):
        return (
            f"<Exposure (exp_no={self.exp_no}, error={self.error}, "
            f"reading={self.reading}, done={self.done()})>"
        )

    async def expose(
        self,
        exposure_time: float | None = None,
        header: str | None = None,
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
            The header JSON string to pass to the ``lvmscp expose`` command.
        async_readout
            Returns after integration completes. Readout is initiated
            but handled asynchronously and can be await by awaiting
            the returned `.Exposure` object.
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
                error_code=3,
            )

        warnings.filterwarnings("ignore", message=".*cannot modify a done command.*")

        self.spec_set.last_exposure = self

        monitor_task: asyncio.Task | None = None

        try:
            await self.spec_set._send_command_all(
                "expose",
                exposure_time=exposure_time,
                seqno=self.exp_no,
                header=(header or "{}"),
                async_readout=async_readout,
                **kwargs,
            )

            self.reading = True

            # Now launch the task that marks the Future done when the spec
            # is IDLE. If async_readout=False then that will return immediately
            # because the spec is already idle. If async_readout=True, this method
            # will return now and the task will mark the Future done when readout
            # complete (readout is ongoing and does not need to be launched).
            monitor_task = asyncio.create_task(self._done_monitor())
            if not async_readout:
                await monitor_task
            else:
                self.spec_set.write_to_log("Returning with async readout ongoing.")

        except Exception as err:
            # Cancel the monitor task
            if monitor_task and not monitor_task.done():
                monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor_task

            await self.stop_timer()
            self.error = True

            raise GortSpecError(f"Exposure failed with error {err}", error_code=301)

        return self

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

                self._progress.refresh()
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

        if self._progress:
            self._progress.stop()
            self._progress.console.clear_live()
            self._progress = None

        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._timer_task
        self._timer_task = None

    def get_files(self):
        """Returns the files written by the exposure."""

        sjd = get_sjd()
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


class IEB(GortDevice):
    """A class representing an Instrument Electronics Box."""

    def __init__(self, gort: GortClient, name: str, actor: str):
        super().__init__(gort, name, actor)

        self.spec_name = self.name.split(".")[1]

    async def status(self):
        """Returns the status of the IEB."""

        replies: list[ActorReply] = await asyncio.gather(
            *[
                self.actor.commands.shutter.commands.status(),
                self.actor.commands.hartmann.commands.status(),
                self.actor.commands.transducer.commands.status(),
                self.actor.commands.wago.commands.status(),
                self.actor.commands.wago.commands.getpower(),
            ]
        )

        status = {}
        for reply in replies:
            flat_reply = reply.flatten()
            if "transducer" in flat_reply:
                flat_reply = {f"{self.spec_name}_pressures": flat_reply["transducer"]}
            status.update(flat_reply)

        return status

    async def power(self, devices: str | list[str], on: bool = True):
        """Powers on/off the shutter or Hartmann doors.

        Parameters
        ----------
        device
            The device to power on/off. Either ``'shutter'``, ``'hartmann_left'``,
            or ``'hartmann_right'``. Can be a list of devices to modify.
        on
            If `True` powers on the device; otherwise powers it down.

        """

        if isinstance(devices, str):
            devices = [devices]

        tasks = []
        for device in devices:
            if device in ["hl", "left"]:
                device = "hartmann_left"
            elif device in ["hr", "right"]:
                device = "hartmann_right"

            if device not in ["shutter", "hartmann_left", "hartmann_right"]:
                raise GortSpecError(
                    f"Invalid device {device}.",
                    error_code=ErrorCodes.USAGE_ERROR,
                )

            self.write_to_log(f"Powering {'on' if on else 'off'} {device}.", "info")

            tasks.append(
                self.actor.commands.wago.commands.setpower(
                    device,
                    action="ON" if on else "OFF",
                )
            )

        await asyncio.gather(*tasks)

    async def do(self, devices: str | list[str], action: str):
        """Performs an action on a device. Powers the device if needed.

        Parameters
        ----------
        device
            The device to act on. Either ``'shutter'``, ``'hartmann_left'``,
            or ``'hartmann_right'``. Can be a list of devices to modify.
        action
            The action to perform. Can be ``'open'``, ``'close'``, ``'home'``,
            or ``'init'``.

        """

        if isinstance(devices, str):
            devices = [devices]

        if action not in ["open", "close", "home", "init"]:
            raise GortSpecError(
                f"Invalid action {action}.",
                error_code=ErrorCodes.USAGE_ERROR,
            )

        status = await self.status()

        tasks = []
        hartmann_done = False
        for device in devices:
            if device in ["hl", "left"]:
                device = "hartmann_left"
            elif device in ["hr", "right"]:
                device = "hartmann_right"

            if device not in ["shutter", "hartmann_left", "hartmann_right"]:
                raise GortSpecError(
                    f"Invalid device {device}.",
                    error_code=ErrorCodes.USAGE_ERROR,
                )

            self.write_to_log(f"Performing {action!r} on {device}.", "info")

            if status[f"{self.spec_name}_relays"][device] is False:
                self.write_to_log(f"Device {device} is off. Powering it on.", "warning")
                await self.power(device)

            if "hartmann" in device:
                command = getattr(self.actor.commands.hartmann.commands, action)
                if action in ["home", "init"]:
                    if hartmann_done:
                        # Avoid homing/initialising the HD twice.
                        continue
                    tasks.append(command())
                    hartmann_done = True
                else:
                    tasks.append(command(side=device.split("_")[1]))
            else:
                command = getattr(self.actor.commands.shutter.commands, action)
                tasks.append(command())

        await asyncio.gather(*tasks)

    async def open(self, devices: str | list[str]):
        """Opens a device or list of devices.

        Parameters
        ----------
        device
            The device to act on. Either ``'shutter'``, ``'hartmann_left'``,
            or ``'hartmann_right'``. Can be a list of devices to open.

        """

        await self.do(devices, "open")

    async def close(self, devices: str | list[str]):
        """Closes a device or list of devices.

        Parameters
        ----------
        device
            The device to act on. Either ``'shutter'``, ``'hartmann_left'``,
            or ``'hartmann_right'``. Can be a list of devices to close.

        """

        await self.do(devices, "close")

    async def home(self, devices: str | list[str]):
        """Homes a device or list of devices.

        Parameters
        ----------
        device
            The device to act on. Either ``'shutter'``, ``'hartmann_left'``,
            or ``'hartmann_right'``. Can be a list of devices to home.

        """

        await self.do(devices, "home")

    async def init(self, devices: str | list[str], home: bool = True):
        """Initialises a device or list of devices.

        Parameters
        ----------
        device
            The device to act on. Either ``'shutter'``, ``'hartmann_left'``,
            or ``'hartmann_right'``. Can be a list of devices to initialise.
        home
            If `True` homes the devices after initialising them.

        """

        await self.do(devices, "init")

        if home:
            await self.do(devices, "home")


class Spectrograph(GortDevice):
    """Class representing an LVM spectrograph functionality."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

        self.nps = self.gort.nps[name]
        self.ieb = IEB(gort, f"ieb.{self.name}", f"lvmieb.{self.name}")

    async def status(self, simple: bool = False):
        """Retrieves the status of the telescope.

        Parameters
        ----------
        simple
            If `True` returns a short version of the status.

        """

        reply: ActorReply = await self.actor.commands.status(simple=simple)
        flatten_reply = reply.flatten()

        return flatten_reply.get("status", {})

    async def is_idle(self):
        """Returns `True` if the spectrograph is idle and ready to expose."""

        status = await self.status(simple=True)
        names = status["status_names"]
        return "IDLE" in names and "READOUT_PENDING" not in names

    async def is_exposing(self):
        """Returns `True` if the spectrograph is exposing."""

        status = await self.status(simple=True)
        return "EXPOSING" in status["status_names"]

    async def is_reading(self):
        """Returns `True` if the spectrograph is idle and ready to expose."""

        status = await self.status(simple=True)
        return "READING" in status["status_names"]

    async def initialise(self):
        """Initialises the spectrograph and flashes the ACF configuration file."""

        self.write_to_log("Initialising spectrograph and flashing ACF.", "info")
        await self.actor.commands.init()

    async def abort(self):
        """Aborts an ongoing exposure."""

        self.write_to_log("Aborting exposures.", "warning")
        await self.actor.commands.abort()

    async def expose(self, **kwargs):
        """Exposes the spectrograph."""

        if not (await self.is_idle()):
            raise GortSpecError(
                "Spectrographs is not idle. Cannot expose.",
                error_code=301,
            )

        self.write_to_log(f"Exposing spectrograph {self.name}.")

        await self.actor.commands.expose(**kwargs)


class SpectrographSet(GortDeviceSet[Spectrograph]):
    """A set of LVM spectrographs."""

    __DEVICE_CLASS__ = Spectrograph

    def __init__(self, gort: GortClient, data: dict[str, dict], **kwargs):
        super().__init__(gort, data, **kwargs)

        self.last_exposure: Exposure | None = None

    async def status(self, simple: bool = False) -> dict[str, dict]:
        """Collects the status of each spectrograph.

        Parameters
        ----------
        simple
            If `True` returns a short version of the status.

        """

        names = list(self)
        statuses = await self.call_device_method(Spectrograph.status, simple=simple)

        return dict(zip(names, statuses))

    def get_seqno(self):
        """Returns the next exposure sequence number."""

        next_exposure_number_path = config["specs"]["nextExposureNumber"]
        with open(next_exposure_number_path, "r") as fd:
            data = fd.read().strip()
            seqno = int(data) if data != "" else 1

        return seqno

    async def are_idle(self):
        """Returns `True` if all the spectrographs are idle and ready to expose."""

        return all(await self.call_device_method(Spectrograph.is_idle))

    async def expose(
        self,
        exposure_time: float | None = None,
        tile_data: dict | None = None,
        show_progress: bool | None = None,
        async_readout: bool = False,
        **kwargs,
    ):
        """Exposes the spectrographs.

        Parameters
        ----------
        exposure_time
            The exposure time.
        tile_data
            Tile data to add to the headers.
        show_progress
            Displays a progress bar with the elapsed exposure time.
            If `None` (the default), will show the progress bar only
            in interactive sessions.
        async_readout
            Returns after integration completes. Readout is initiated
            but handled asynchronously and can be await by awaiting
            the returned `.Exposure` object.
        kwargs
            Keyword arguments to pass to ``lvmscp expose``.

        Returns
        -------
        exp_nos
            The numbers of the exposed frames.

        """

        if self.last_exposure is not None and not self.last_exposure.done():
            self.write_to_log("Waiting for previous exposure to read out.", "warning")
            await self.last_exposure

        if not (await self.are_idle()):
            raise GortSpecError(
                "Spectrographs are not idle. Cannot expose.",
                error_code=302,
            )

        if "count" in kwargs:
            raise GortSpecError(
                "Count cannot be used here. Use a loop instead.",
                error_code=3,
            )

        if kwargs.get("bias", False) or kwargs.get("flavour", "object") == "bias":
            exposure_time = 0.0

        seqno = self.get_seqno()
        self.write_to_log(f"Taking spectrograph exposure {seqno}.", level="info")

        await self.reset()

        if tile_data is not None:
            header = json.dumps(tile_data)
        else:
            header = None

        if show_progress is None:
            show_progress = is_interactive() or is_notebook()

        exposure = Exposure(seqno, self)
        await exposure.expose(
            exposure_time=exposure_time,
            header=header,
            async_readout=async_readout,
            show_progress=show_progress,
            **kwargs,
        )

        return exposure

    async def reset(self):
        """Reset the spectrographs."""

        await self._send_command_all("reset")

    async def initialise(self):
        """Initialises the spectrographs and flashes the ACF configuration file."""

        await self.call_device_method(Spectrograph.initialise)

    async def abort(self):
        """Aborts an ongoing exposure."""

        await self.call_device_method(Spectrograph.abort)
        self.last_exposure = None

    async def calibrate(
        self,
        sequence: str | dict = "normal",
        park_after: bool = True,
        show_progress: bool = False,
    ):
        """Runs the calibration sequence.

        Parameters
        ----------
        sequence
            The name calibration sequence to execute. It can also be a
            dictionary with the calibration sequence definition that
            follows the :ref:`calibration schema <calibration-schema>`.
        park_after
            Park the telescopes after a successful calibration sequence.
        show_progress
            Displays a progress bar with the elapsed exposure time.

        """

        # TODO: add some checks. Confirm HDs are open, enclosure is closed,
        # specs connected, etc.

        # Calibration sequence configuration. Includes the position where to
        # point the telescopes, NPS to use, and sequences.
        cal_config = config["specs"]["calibration"]

        # Task that will move the fibre selector.
        fibsel_task: asyncio.Task | None = None

        sequence_config: dict[str, Any]
        if isinstance(sequence, dict):
            sequence_config = sequence

        else:
            if sequence not in cal_config["sequences"]:
                raise GortSpecError(f"Unknown sequence {sequence!r}.", error_code=303)
            sequence_config = cal_config["sequences"][sequence]

        # Validate sequence.
        schema_file = pathlib.Path(__file__).parent / "../etc/calibration_schema.json"
        schema = json.loads(open(schema_file).read())
        try:
            jsonschema.validate(sequence_config, schema)
        except jsonschema.ValidationError:
            raise GortSpecError(
                "Calibration sequence does not match schema.",
                error_code=ErrorCodes.INVALID_CALIBRATION_SEQUENCE,
            )

        calib_nps = self.gort.nps[cal_config["lamps_nps"]]
        lamps_config = sequence_config.get("lamps", {})

        # Move the telescopes to point to the screen.
        self.write_to_log("Moving telescopes to position.", level="info")
        await self.gort.telescopes.goto_named_position(cal_config["position"])

        # Turn off all lamps.
        self.write_to_log("Checking that all lamps are off.", level="info")
        await calib_nps.all_off()

        self.write_to_log(f"Running calibration sequence {sequence!r}.", level="info")

        try:
            if "biases" in sequence_config:
                self.write_to_log("Taking biases.", level="info")
                nbias = sequence_config["biases"].get("count", 1)
                for _ in range(nbias):
                    await self.gort.specs.expose(flavour="bias")

            if "darks" in sequence_config:
                self.write_to_log("Taking darks.", level="info")
                ndarks = sequence_config["darks"].get("count")
                for idark in range(ndarks):
                    await self.gort.specs.expose(
                        flavour="dark",
                        exposure_time=sequence_config["darks"]["exposure_time"],
                        async_readout=idark == ndarks - 1,
                    )

            for lamp in lamps_config:
                warmup = lamps_config[lamp]["warmup"]

                self.write_to_log(f"Warming up lamp {lamp} ({warmup} s).", level="info")
                await calib_nps.on(lamp)
                await asyncio.sleep(warmup)

                exp_times = lamps_config[lamp]["exposure_times"]
                n_exp_times = len(exp_times)
                for ietime, exp_time in enumerate(exp_times):
                    flavour = lamps_config[lamp]["flavour"]

                    # Check if we are spinning the fibre selector and,
                    # if so, launch the task.
                    fibsel = lamps_config[lamp].get("fibsel", None)
                    if fibsel:
                        initial_position = fibsel.get("initial_position", None)
                        positions = fibsel.get("positions", "P1-")
                        time_per_position = fibsel.get("time_per_position", None)
                        total_time = exp_time if time_per_position is None else None

                        if initial_position:
                            # Move to the initial position before starting the exposure.
                            await self.gort.telescopes.spec.fibsel.move_to_position(
                                initial_position
                            )

                        # Launch the task.
                        fibsel_task = asyncio.create_task(
                            move_mask_interval(
                                self.gort,
                                positions,
                                order_by_steps=True,
                                total_time=total_time,
                                time_per_position=time_per_position,
                            )
                        )

                    self.write_to_log(f"Exposing for {exp_time} s.", level="info")
                    await self.gort.specs.expose(
                        flavour=flavour,
                        exposure_time=exp_time,
                        show_progress=show_progress,
                        async_readout=ietime == n_exp_times - 1,
                    )

                    if fibsel_task:
                        await fibsel_task

                self.write_to_log(f"Turning off {lamp}.")
                await calib_nps.off(lamp)

            if park_after:
                await self.gort.telescopes.park()

            if self.last_exposure and not self.last_exposure.done():
                self.write_to_log("Awaiting last exposure readout.")
                await self.last_exposure

        except Exception:
            self.write_to_log(
                "Errored while executing sequence. "
                "Turning all the lamps off before raising.",
                level="error",
            )

            # Stop the mask iteration task.
            if fibsel_task and not fibsel_task.done():
                fibsel_task.cancel()

            raise

        finally:
            await calib_nps.all_off()
            await calib_nps.all_off()

    def get_calibration_sequence(self, sequence: str):
        """Returns a dictionary with the configuration for a calibration sequence.

        Parameters
        ----------
        sequence
            The name calibration sequence.

        Returns
        -------
        sequence_dict
            The calibration sequence dictionary. This dictionary can be
            altered and then passed to :obj:`.calibrate` to execute the
            modified sequence. The returned dictionary if a deep copy of
            the original sequence; modifying it won't modify the original
            sequence.

        """

        sequences = config["specs"]["calibration"]["sequences"]

        if sequence not in sequences:
            raise ValueError(f"Sequence {sequence!r} not found in configuration file.")

        return deepcopy(sequences[sequence])
