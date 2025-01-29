#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-18
# @Filename: enclosure.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Literal

from lvmopstools import Retrier

from gort.devices.core import GortDevice, GortDeviceSet
from gort.enums import Event
from gort.exceptions import ErrorCode, GortEnclosureError, GortTelescopeError
from gort.gort import Gort


if TYPE_CHECKING:
    from gort import Gort


if TYPE_CHECKING:
    from gort import ActorReply


__all__ = ["Enclosure", "Lights", "Light"]


class Light:
    """An enclosure light."""

    def __init__(self, enclosure: Enclosure, name: str):
        self.enclosure = enclosure
        self.name = name

    @Retrier(max_attempts=3, delay=1)
    async def on(self):
        """Turns on the light."""

        status = await self.status()
        if status is False:
            self.enclosure.write_to_log(f"Turning on light {self.name!r}.", "info")
            await self.enclosure.actor.commands.lights("on", self.name)

    @Retrier(max_attempts=3, delay=1)
    async def off(self):
        """Turns on the light."""

        status = await self.status()
        if status is True:
            self.enclosure.write_to_log(f"Turning off light {self.name!r}.", "info")
            await self.enclosure.actor.commands.lights("off", self.name)

    @Retrier(max_attempts=3, delay=1)
    async def toggle(self):
        """Turns on the light."""

        self.enclosure.write_to_log(f"Toggling {self.name!r}.", "info")
        await self.enclosure.actor.commands.lights("toggle", self.name)

    @Retrier(max_attempts=3, delay=1)
    async def status(self) -> bool:
        """Returns a boolean with the light status."""

        status = await self.enclosure.actor.commands.lights("status")

        labels = status.get("lights_labels")

        if labels is None:
            raise GortEnclosureError("Did not receive lights status.")

        return self.name.upper() in labels


class Lights:
    """Controls the enclosure lights."""

    LIGHTS = [
        "telescope_bright",
        "telescope_red",
        "spectrograph_room",
        "utilities_room",
    ]

    def __init__(self, enclosure: Enclosure):
        for light in self.LIGHTS:
            self.__setattr__(light, Light(enclosure, light))

    def __repr__(self):
        return f"<Lights ({', '.join(self.LIGHTS)})>"

    @Retrier(max_attempts=3, delay=1)
    async def dome_all_off(self):
        """Turns off all the lights in the dome."""

        await asyncio.gather(self.telescope_bright.off(), self.telescope_red.off())

    if TYPE_CHECKING:

        def __getattr__(self, light: str) -> Light: ...


class E_Stops:
    """Reports status of the emergency stops buttons and allows to reset the relays."""

    def __init__(self, enclosure: Enclosure):
        self.enclosure = enclosure
        self.ecp = enclosure.actor
        self.gort = enclosure.gort

    @Retrier(max_attempts=3, delay=0.5)
    async def status(self):
        """Returns :obj:`True` if the emergency stops are pressed."""

        status = await self.enclosure.status()
        labels = status.get("safety_status_labels", "").split(",")

        if labels is None:
            raise GortEnclosureError("Cannot determine the status of the e-stops.")

        return "E_STOP" in labels

    @Retrier(max_attempts=3, delay=0.5)
    async def trigger(self):
        """Triggers the e-stop relays."""

        try:
            await self.ecp.commands.emergency_stop()
        except Exception as err:
            raise GortEnclosureError(f"Failed to trigger the e-stops: {err}")

    @Retrier(max_attempts=3, delay=0.5)
    async def reset(self):
        """Resets the e-stop relays after the e-stops have been released."""

        try:
            await self.ecp.commands.engineering_mode.commands.reset_e_stops()
        except Exception as err:
            raise GortEnclosureError(f"Failed to reset the e-stops: {err}")


class Enclosure(GortDevice):
    """Class representing the LVM enclosure."""

    __DEPLOYMENTS__ = ["lvmecp"]

    def __init__(self, gort: Gort, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

        self.lights = Lights(self)
        self.e_stops = E_Stops(self)

        self._dome_lock = asyncio.Lock()

    async def restart(self):
        """Restarts the ``lvmecp`` deployment."""

        await GortDeviceSet.restart(self)  # type: ignore

    @Retrier(max_attempts=3, delay=1)
    async def status(self, get_registers: bool = False):
        """Retrieves the status of the power outlet."""

        reply: ActorReply = await self.actor.commands.status(
            no_registers=(not get_registers),
            timeout=5,
        )

        return reply.flatten()

    async def allowed_to_move(self):
        """Checks if the dome is allowed to move."""

        status: ActorReply = await self.actor.commands.dome.commands.status(timeout=5)
        labels = status.get("dome_status_labels", default="").split(",")

        if "DRIVE_ERROR" in labels:
            return False

        if "UNKNOWN" in labels:
            return False

        if "DRIVE_AVAILABLE" not in labels:
            return False

        if await self.is_local():
            return False

        return True

    async def _park_telescopes(self):
        """Moves telescopes to park position before opening/closing the enclosure."""

        telescopes = list(self.gort.telescopes)
        is_parked = await asyncio.gather(
            *[self.gort.telescopes[tel].is_parked() for tel in telescopes]
        )

        if all(is_parked):
            return True

        self.write_to_log(
            "Moving telescopes to park before operating the dome.",
            "warning",
        )

        # Check local only once here.
        if await self.gort.enclosure.is_local():
            raise GortTelescopeError(
                "Cannot move telescope in local mode.",
                error_code=ErrorCode.CANNOT_MOVE_LOCAL_MODE,
            )

        park_coros = [
            self.gort.telescopes[tel].goto_named_position("park", force=True)
            for itel, tel in enumerate(telescopes)
            if not is_parked[itel]
        ]

        await asyncio.gather(*park_coros)

    async def _check_dome(self):
        """Checks if we can operate the dome or raises an exception."""

        if await self.allowed_to_move():
            return

        if await self.is_local():
            raise GortEnclosureError(
                "Cannot open the enclosure while in local mode.",
                error_code=ErrorCode.ENCLOSURE_IN_LOCAL,
            )
        else:
            raise GortEnclosureError(
                "Not allowed to move the dome.",
                error_code=ErrorCode.ENCLOSURE_ERROR,
            )

    async def open(self, park_telescopes: bool = True):
        """Open the enclosure dome.

        Parameters
        ----------
        park_telescopes
            Move the telescopes to the park position before opening the
            enclosure to prevent dust or other debris falling on them.

        """

        async with self._dome_lock:
            if await self.is_open():
                self.write_to_log("Dome is already open.", level="info")
                return

            await self._check_dome()

            if park_telescopes:
                await asyncio.wait_for(self._park_telescopes(), timeout=120)

            self.write_to_log("Opening the enclosure ...", level="info")
            await self.gort.notify_event(Event.DOME_OPENING)

            await self.actor.commands.dome.commands.open()

            self.write_to_log("Enclosure is now open.", level="info")
            await self.gort.notify_event(Event.DOME_OPEN)

    async def close(
        self,
        park_telescopes: bool = True,
        force: bool = False,
        mode: Literal["normal", "overcurrent"] = "normal",
    ):
        """Close the enclosure dome.

        Parameters
        ----------
        park_telescopes
            Move the telescopes to the park position before closing the
            enclosure to prevent dust or other debris falling on them.
        force
            Tries to closes the dome even if the system believes it is
            already closed.
        mode
            In ``'normal'`` mode the dome will be closed using a combination of
            rotary encoder and limit switches. With ``'overcurrent'`` the dome
            is closed until the VFD detects an overcurrent condition.
            ``'overcurrent'`` should only be used when the normal mode has failed.

        """

        async with self._dome_lock:
            if not force and await self.is_closed():
                self.write_to_log("Dome is already closed.", level="info")
                return

            await self._check_dome()

            if park_telescopes:
                try:
                    await asyncio.wait_for(self._park_telescopes(), timeout=120)
                except Exception as err:
                    self.write_to_log(
                        f"Failed parking the telescopes: {err}",
                        "error",
                        exc_info=err,
                    )
                    if force is False:
                        raise GortEnclosureError(
                            "Not closing without knowing where the telescopes are. "
                            "If you really need to close call again with "
                            "park_telescopes=False and force=True.",
                        )
                    else:
                        self.write_to_log("Closing dome because force=True", "warning")

            self.write_to_log("Closing the dome ...", level="info")
            await self.gort.notify_event(Event.DOME_CLOSING)

            await self.actor.commands.dome.commands.close(
                force=force,
                overcurrent=(mode == "overcurrent"),
            )

            self.write_to_log("Enclosure is now closed.", level="info")
            await self.gort.notify_event(Event.DOME_CLOSED)

    async def is_open(self):
        """Returns :obj:`True` if the enclosure is open."""

        status = await self.status()
        labels = status["dome_status_labels"].split(",")

        return "OPEN" in labels and "MOVING" not in labels

    async def is_closed(self):
        """Returns :obj:`True` if the enclosure is closed."""

        status = await self.status()
        labels = status["dome_status_labels"].split(",")

        return "CLOSED" in labels and "MOVING" not in labels

    @Retrier(max_attempts=2, delay=1)
    async def stop(self):
        """Stop the enclosure dome."""

        self.write_to_log("Stoping the dome.", level="info")
        await self.actor.commands.dome.commands.stop()

    async def is_local(self):
        """Returns :obj:`True` if the enclosure is in local mode."""

        # This should generally not be on, but it's useful as a way of disabling
        # the local mode when the lock or door are not working.
        if self.gort.config["enclosure"].get("bypass_local_mode", False) is True:
            return False

        status = await self.status()
        safety_status_labels = status.get("safety_status_labels", None)
        if safety_status_labels is None:
            raise GortEnclosureError(
                "Cannot determine if enclosure is in local mode.",
                error_code=ErrorCode.ENCLOSURE_IN_LOCAL,
            )

        return "LOCAL" in safety_status_labels

    async def get_door_status(self):
        """Returns the status of the door and lock."""

        status = await self.status()
        safety_status_labels = status.get("safety_status_labels", None)
        if safety_status_labels is None:
            raise GortEnclosureError(
                "Cannot determine door status.",
                error_code=ErrorCode.DOOR_STATUS_FAILED,
            )

        reply = {
            "door_closed": "DOOR_CLOSED" in safety_status_labels,
            "door_locked": "DOOR_LOCKED" in safety_status_labels,
            "local": await self.is_local(),
        }

        return reply
