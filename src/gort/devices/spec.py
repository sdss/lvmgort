#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-15
# @Filename: spec.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Sequence

from gort.exceptions import ErrorCode, GortError, GortSpecError
from gort.exposure import Exposure
from gort.gort import GortDevice, GortDeviceSet


if TYPE_CHECKING:
    from gort.core import ActorReply
    from gort.gort import GortClient


__all__ = ["Spectrograph", "SpectrographSet", "IEB"]


class IEB(GortDevice):
    """A class representing an Instrument Electronics Box."""

    def __init__(self, gort: GortClient, name: str, actor: str):
        super().__init__(gort, name, actor)

        self.spec_name = self.name.split(".")[1]

    async def status(self):
        """Returns the status of the IEB."""

        replies: list[ActorReply] = await asyncio.gather(
            self.actor.commands.shutter.commands.status(),
            self.actor.commands.hartmann.commands.status(),
            self.actor.commands.transducer.commands.status(),
            self.actor.commands.wago.commands.status(),
            self.actor.commands.wago.commands.getpower(),
            return_exceptions=True,
        )

        status = {}
        for reply in replies:
            if isinstance(reply, Exception):
                self.write_to_log(str(reply), "warning")
                continue

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
            If :obj:`True` powers on the device; otherwise powers it down.

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
                    error_code=ErrorCode.USAGE_ERROR,
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
                error_code=ErrorCode.USAGE_ERROR,
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
                    error_code=ErrorCode.USAGE_ERROR,
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
            If :obj:`True` homes the devices after initialising them.

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
            If :obj:`True` returns a short version of the status.

        """

        reply: ActorReply = await self.actor.commands.status(simple=simple)
        flatten_reply = reply.flatten()

        return flatten_reply.get("status", {})

    async def is_idle(self):
        """Returns :obj:`True` if the spectrograph is idle and ready to expose."""

        status = await self.status(simple=True)
        names = status["status_names"]

        for status in ["READOUT_PENDING", "ERROR"]:
            if status in names:
                return False

        return "IDLE" in names

    async def is_errored(self):
        """Returns :obj:`True` if the spectrograph is in error."""

        status = await self.status(simple=True)
        return "ERROR" in status["status_names"]

    async def is_exposing(self):
        """Returns :obj:`True` if the spectrograph is exposing."""

        status = await self.status(simple=True)
        return "EXPOSING" in status["status_names"]

    async def is_reading(self):
        """Returns :obj:`True` if the spectrograph is idle and ready to expose."""

        status = await self.status(simple=True)
        return "READING" in status["status_names"]

    async def initialise(self):
        """Initialises the spectrograph and flashes the ACF configuration file."""

        self.write_to_log("Initialising spectrograph and flashing ACF.", "info")
        await self.actor.commands.init()

    async def abort(self):
        """Aborts an ongoing exposure."""

        self.write_to_log("Aborting exposures.", "warning")
        try:
            await self.actor.commands.abort()
        except GortError:
            pass

        await self.actor.commands.reset()

        self.write_to_log("Closing shutter.")
        await self.ieb.close("shutter")

    async def reset(self, full: bool = False):
        """Resets the spectrograph to a valid state."""

        await self.actor.commands.reset()

        mech_status = await self.ieb.status()

        # Always try to close the shutter. If this fails, try a full reset in
        # part to give the shutter another chance but also because it could be
        # that it failed because the power to the motor controllers is off.
        if full is False and mech_status[f"{self.name}_shutter"]["open"]:
            self.write_to_log(f"Closing {self.name} shutter.", "warning")
            try:
                await self.ieb.close("shutter")
            except Exception as ee:
                self.write_to_log(f"Failed closing the shutter: {ee}", "warning")
                self.write_to_log("Trying a full reset.", "warning")
                full = True

        if full:
            relays = mech_status[f"{self.name}_relays"]
            if not all(relays.values()):
                self.write_to_log(f"Powering on {self.name} relays.", "warning")
                await self.ieb.power(list(relays.keys()))
                mech_status = await self.ieb.status()

            if mech_status[f"{self.name}_shutter"]["open"]:
                self.write_to_log(f"Closing {self.name} shutter.", "warning")
                await self.ieb.close("shutter")

            for side in ["left", "right"]:
                if not mech_status[f"{self.name}_hartmann_{side}"]["open"]:
                    self.write_to_log(f"Opening {self.name} {side} HD.", "warning")
                    await self.ieb.open(f"hartmann_{side}")

    async def expose(self, **kwargs):
        """Exposes the spectrograph."""

        if not (await self.is_idle()):
            raise GortSpecError(
                "Spectrographs is not idle. Cannot expose.",
                error_code=ErrorCode.SECTROGRAPH_FAILED_EXPOSING,
            )

        self.write_to_log(f"Exposing spectrograph {self.name}.")

        await self.actor.commands.expose(**kwargs)


class SpectrographSet(GortDeviceSet[Spectrograph]):
    """A set of LVM spectrographs."""

    __DEVICE_CLASS__ = Spectrograph
    __DEPLOYMENTS__ = ["lvmscp"]

    def __init__(self, gort: GortClient, data: dict[str, dict], **kwargs):
        super().__init__(gort, data, **kwargs)

        self.last_exposure: Exposure | None = None

    async def status(self, simple: bool = False) -> dict[str, dict]:
        """Collects the status of each spectrograph.

        Parameters
        ----------
        simple
            If :obj:`True` returns a short version of the status.

        """

        names = list(self)
        statuses = await self.call_device_method(Spectrograph.status, simple=simple)

        return dict(zip(names, statuses))

    def get_expno(self):
        """Returns the next exposure sequence number."""

        next_exposure_number_path = self.gort.config["specs"]["nextExposureNumber"]
        with open(next_exposure_number_path, "r") as fd:
            data = fd.read().strip()
            expno = int(data) if data != "" else 1

        return expno

    async def are_idle(self):
        """Returns :obj:`True` if all the spectrographs are idle and ready to expose."""

        return all(await self.call_device_method(Spectrograph.is_idle))

    async def are_errored(self):
        """Returns :obj:`True` if any of the spectrographs are errored."""

        return any(await self.call_device_method(Spectrograph.is_errored))

    async def expose(
        self,
        exposure_time: float | None = None,
        flavour: str | None = None,
        header: dict | None = None,
        show_progress: bool | None = None,
        async_readout: bool = False,
        count: int = 1,
        object: str | None = None,
        specs: Sequence[str] | None = None,
        **kwargs,
    ) -> Exposure | list[Exposure]:
        """Exposes the spectrographs.

        Parameters
        ----------
        exposure_time
            The exposure time. If not set, assumes this must
            be a bias.
        flavour
            The exposure type, either ``'object'``, ``'arc'``,
            ``'flat'``, ``'dark'``, or ``'bias'``
        header
            Additional data to add to the headers.
        show_progress
            Displays a progress bar with the elapsed exposure time.
            If :obj:`None` (the default), will show the progress bar only
            in interactive sessions.
        async_readout
            Returns after integration completes. Readout is initiated
            but handled asynchronously and can be await by awaiting
            the returned :obj:`.Exposure` object.
        count
            The number of exposures to take.
        object
            A string that will be stored in the ``OBJECT`` header
            keyword.
        specs
            List the spectrographs to expose. Defaults to all.

        Returns
        -------
        exp_nos
            The numbers of the exposed frames. If ``count`` is greater than
            one, returns a list of exposures.

        """

        if self.last_exposure is not None and not self.last_exposure.done():
            self.write_to_log("Waiting for previous exposure to read out.", "warning")
            await self.last_exposure

        if not (await self.are_idle()):
            raise GortSpecError(
                "Spectrographs are not idle. Cannot expose.",
                error_code=ErrorCode.SECTROGRAPH_NOT_IDLE,
            )

        if count <= 0:
            raise GortSpecError("Invalid count.", error_code=ErrorCode.USAGE_ERROR)

        if exposure_time is None or exposure_time == 0.0:
            flavour = "bias"
            exposure_time = 0.0

        exposures: list[Exposure] = []

        for nexp in range(int(count)):
            exposure = Exposure(self.gort, flavour=flavour, object=object, specs=specs)

            async_readout_this_exp = async_readout if nexp == int(count) - 1 else False

            await exposure.expose(
                exposure_time=exposure_time,
                header=header,
                async_readout=async_readout_this_exp,
                show_progress=show_progress,
                **kwargs,
            )
            exposures.append(exposure)

        if async_readout:
            self.write_to_log("Returning with async readout ongoing.")

        if len(exposures) == 1:
            return exposures[0]

        return exposures

    async def reset(self, full: bool = False):
        """Reset the spectrographs."""

        await self.call_device_method(Spectrograph.reset, full=full)

        self.last_exposure = None

    async def initialise(self):
        """Initialises the spectrographs and flashes the ACF configuration file."""

        await self.call_device_method(Spectrograph.initialise)

    async def abort(self):
        """Aborts an ongoing exposure."""

        await self.call_device_method(Spectrograph.abort)
        self.last_exposure = None
