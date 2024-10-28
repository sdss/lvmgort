#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-10
# @Filename: telescope.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from collections import defaultdict
from time import time

from typing import TYPE_CHECKING, ClassVar

import numpy

from gort.exceptions import ErrorCode, GortTelescopeError
from gort.gort import GortClient, GortDevice, GortDeviceSet
from gort.tools import angular_separation, kubernetes_restart_deployment


if TYPE_CHECKING:
    from gort.core import ActorReply


__all__ = ["Telescope", "TelescopeSet", "KMirror", "FibSel", "Focuser", "MoTanDevice"]


class MoTanDevice(GortDevice):
    """A TwiceAsNice device."""

    #: Artificial delay introduced to prevent all motors to slew at the same time.
    SLEW_DELAY: ClassVar[float | dict[str, float]] = 0

    def __init__(self, gort: GortClient, name: str, actor: str):
        super().__init__(gort, name, actor)

        class_name = self.__class__.__name__

        timeouts = self.gort.config["telescopes"]["timeouts"].get(class_name.lower())

        self.timeouts: defaultdict[str, float | None]
        self.timeouts = defaultdict(lambda: None, timeouts)

    async def is_reachable(self):
        """Is the device reachable?"""

        is_reachable = await self.run_command("isReachable")

        return bool(is_reachable.get("Reachable"))

    async def is_moving(self):
        """Is the device moving."""

        is_moving = await self.run_command("isMoving")

        return bool(is_moving.get("Moving"))

    async def slew_delay(self):
        """Sleeps the :obj:`.SLEW_DELAY` amount."""

        if isinstance(self.SLEW_DELAY, (float, int)):
            await asyncio.sleep(self.SLEW_DELAY)
        else:
            await asyncio.sleep(self.SLEW_DELAY[self.name.split(".")[0]])

    async def run_command(
        self,
        command: str,
        *args,
        retries: int = 3,
        delay: float = 1,
        **kwargs,
    ) -> ActorReply:
        """Runs a MoTan command with retries."""

        self.actor.commands[command].set_retries(retries, retry_delay=delay)

        return await self.actor.commands[command](*args, **kwargs)


class KMirror(MoTanDevice):
    """A device representing a K-mirror."""

    SLEW_DELAY = 0

    async def status(self):
        """Returns the status of the k-mirror."""

        return await self.run_command("status")

    async def home(self):
        """Homes the k-mirror."""

        if not (await self.is_reachable()):
            raise GortTelescopeError("Device is not reachable.")

        await self.slew_delay()
        await self.run_command("slewStop", timeout=self.timeouts["slewStop"])

        self.write_to_log("Homing k-mirror.", level="info")
        await self.run_command("moveToHome", timeout=self.timeouts["moveToHome"])
        self.write_to_log("k-mirror homing complete.")

    async def park(self):
        """Park the k-mirror at 90 degrees."""

        if not (await self.is_reachable()):
            raise GortTelescopeError("Device is not reachable.")

        await self.slew_delay()

        await self.move(90)

    async def move(self, degs: float):
        """Move the k-mirror to a position in degrees. Does NOT track after the move.

        Parameters
        ----------
        degs
            The position to which to move the k-mirror, in degrees.

        """

        if not (await self.is_reachable()):
            raise GortTelescopeError("Device is not reachable.")

        await self.slew_delay()

        self.write_to_log(f"Moving k-mirror to {degs:.3f} degrees.", level="info")

        self.write_to_log("Stopping slew.")
        await self.run_command("slewStop", timeout=self.timeouts["slewStop"])

        self.write_to_log("Moving k-mirror to absolute position.")
        await self.run_command(
            "moveAbsolute",
            degs,
            "deg",
            timeout=self.timeouts["moveAbsolute"],
        )

    async def slew(
        self,
        ra: float,
        dec: float,
        offset_angle: float = 0.0,
        stop_degs_before: float = 0.0,
    ):
        """Moves the mirror to the position for ``ra, dec`` and starts slewing.

        Parameters
        ----------
        ra
            Right ascension of the field to track, in degrees.
        dec
            Declination of the field to track, in degrees.
        offset_angle
            Derotation offset in degrees. This value is converted to the
            -180 to 180 deg range before sending it to the k-mirror.
        stop_degs_before
            Number of degrees to stop before reaching the desired position
            angle. This has the effect of actually slewing to
            ``offset_angle-stop_degs_before``. Useful if we want to be
            sure that positive offsets will be applied without backlash.

        """

        if not (await self.is_reachable()):
            raise GortTelescopeError("Device is not reachable.")

        await self.slew_delay()

        offset_angle %= 360
        if offset_angle > 180:
            offset_angle -= 360

        if offset_angle == 0:
            msg = f"Slewing k-mirror to ra={ra:.6f} dec={dec:.6f} and tracking."
        else:
            msg = (
                f"Slewing k-mirror to ra={ra:.6f} dec={dec:.6f} "
                f"pa={offset_angle:.3f} and tracking."
            )

        self.write_to_log(msg, level="info")

        stop_degs_before = abs(stop_degs_before)
        if abs(stop_degs_before) > 0:
            self.write_to_log(f"Using stop_degs_before={stop_degs_before}.")

        await self.run_command(
            "slewStart",
            ra / 15.0,
            dec,
            seg_time=self.gort.config["telescopes"]["kmirror"]["seg_time"],
            seg_min_num=self.gort.config["telescopes"]["kmirror"]["seg_min_num"],
            offset_angle=offset_angle - stop_degs_before,
            timeout=self.timeouts["slewStart"],
        )


class Focuser(MoTanDevice):
    """A device representing a focuser."""

    SLEW_DELAY = 0

    async def status(self):
        """Returns the status of the focuser."""

        return await self.run_command("status")

    async def home(self):
        """Homes the focuser.

        Parameters
        ----------
        restore_position
            Whether to restore the previous focuser position after homing.

        """

        if not (await self.is_reachable()):
            raise GortTelescopeError("Device is not reachable.")

        # Store current position to restore it later.
        status = await self.status()
        current_position = status.get("Position")

        await self.slew_delay()

        self.write_to_log("Homing focuser.", level="info")
        await self.run_command("moveToHome", timeout=self.timeouts["moveToHome"])
        self.write_to_log("Focuser homing complete.")

        if current_position is not None and not numpy.isnan(current_position):
            self.write_to_log(f"Restoring position {current_position} DT.")
            await self.move(current_position)

    async def move(self, dts: float):
        """Move the focuser to a position in DT."""

        if not (await self.is_reachable()):
            raise GortTelescopeError("Device is not reachable.")

        await self.slew_delay()

        self.write_to_log(f"Moving focuser to {dts:.3f} DT.", level="info")
        await self.run_command(
            "moveAbsolute",
            dts,
            "DT",
            timeout=self.timeouts["moveAbsolute"],
        )


class FibSel(MoTanDevice):
    """A device representing the fibre mask in the spectrophotometric telescope."""

    # We really don't want a delay here because it would slow down the acquisition
    # of new standards, and anyway the fibre selector usually moves by itself.
    SLEW_DELAY: float = 0

    # Rehome after this many seconds.
    HOME_AFTER: float | None = None

    def __init__(self, gort: GortClient, name: str, actor: str):
        super().__init__(gort, name, actor)

        self.__last_homing: float = 0

    async def status(self):
        """Returns the status of the fibre selector."""

        return await self.run_command("status")

    async def home(self):
        """Homes the fibre selector."""

        if not (await self.is_reachable()):
            raise GortTelescopeError("Device is not reachable.")

        await self.slew_delay()

        self.write_to_log("Homing fibsel.", level="info")
        await self.run_command("moveToHome", timeout=self.timeouts["moveToHome"])
        self.write_to_log("Fibsel homing complete.")

        self.__last_homing = time()

    def list_positions(self) -> list[str]:
        """Returns a list of valid positions."""

        return list(self.gort.config["telescopes"]["mask_positions"])

    async def _check_home(self):
        """Checks if a homing is required before moving the mask."""

        if self.HOME_AFTER is None:
            return

        if time() - self.__last_homing > self.HOME_AFTER:
            self.write_to_log("Rehoming fibsel before moving.", "warning")
            await self.home()

    async def move_to_position(self, position: str | int, rehome: bool = False):
        """Moves the spectrophotometric mask to the desired position.

        Parameters
        ----------
        position
            A position in the form `PN-M` where ``N=1,2`` and ``M=1-12``, in which
            case the mask will rotate to expose the fibre with that name. If
            ``position`` is a number, moves the mask to that value.
        rehome
            Home the fibre selector before moving to the new position.

        """

        if not (await self.is_reachable()):
            raise GortTelescopeError("Device is not reachable.")

        if rehome:
            await self.home()
        else:
            await self._check_home()

        if isinstance(position, str):
            mask_positions = self.gort.config["telescopes"]["mask_positions"]
            if position not in mask_positions:
                raise GortTelescopeError(
                    f"Cannot find position {position!r}.",
                    error_code=ErrorCode.FIBSEL_INVALID_POSITION,
                )

            steps = mask_positions[position]
            self.write_to_log(f"Moving mask to {position}: {steps} DT.", level="info")

        else:
            steps = position
            self.write_to_log(f"Moving mask to {steps} DT.", level="info")

        await self.slew_delay()
        await self.run_command(
            "moveAbsolute",
            steps,
            timeout=self.timeouts["moveAbsolute"],
        )

    async def move_relative(self, steps: float):
        """Move the mask a number of motor steps relative to the current position."""

        if not (await self.is_reachable()):
            raise GortTelescopeError("Device is not reachable.")

        self.write_to_log(f"Moving fibre mask {steps} steps.")

        await self.slew_delay()
        await self._check_home()

        await self.run_command(
            "moveRelative",
            steps,
            timeout=self.timeouts["moveRelative"],
        )


class Telescope(GortDevice):
    """Class representing an LVM telescope functionality."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

        self.is_homed: bool = False

        self.config = self.gort.config["telescopes"]
        self.pwi = self.actor

        kmirror_actor = kwargs.get("kmirror", None)
        if kmirror_actor:
            self.has_kmirror = True
            self.km = KMirror(self.gort, f"{self.name}.km", kmirror_actor)
        else:
            self.has_kmirror = False
            self.km = None

        self.focuser = Focuser(self.gort, f"{self.name}.focuser", kwargs["focuser"])

        self.fibsel = (
            FibSel(self.gort, f"{self.name}.fibsel", "lvm.spec.fibsel")
            if self.name == "spec"
            else None
        )

        if self.name in self.gort.guiders:
            self.guider = self.gort.guiders[name]
        else:
            self.guider = None

        self.timeouts = self.gort.config["telescopes"]["timeouts"]["pwi"]

    async def init(self):
        """Determines the initial state of the telescope."""

        # If the axes are enabled, we assume the telescope is homed.
        if not self.is_homed and (await self.is_ready()):
            self.is_homed = True

    async def status(self):
        """Retrieves the status of the telescope."""

        reply: ActorReply = await self.pwi.commands.status()
        return reply.flatten()

    async def is_parked(self):
        """Is the telescope parked?"""

        park_position = self.gort.config["telescopes"]["named_positions"]["park"]["all"]

        status = await self.status()
        if status["is_enabled"] or status["is_tracking"] or status["is_slewing"]:
            return False

        alt_diff = numpy.abs(status["altitude_degs"] - park_position["alt"])
        az_diff = numpy.abs(status["azimuth_degs"] - park_position["az"])

        if alt_diff > 5 or az_diff > 5:
            return False

        return True

    async def is_ready(self):
        """Checks if the telescope is ready to be moved."""

        status = await self.status()

        is_connected = status.get("is_connected", False)
        is_enabled = status.get("is_enabled", False)

        return is_connected and is_enabled

    async def initialise(self, home: bool | None = None):
        """Connects to the telescope and initialises the axes.

        Parameters
        ----------
        home
            If :obj:`True`, runs the homing routine after initialising.

        """

        if not (await self.is_ready()):
            self.write_to_log("Initialising telescope.")
            await self.pwi.commands.setConnected(True)
            await self.pwi.commands.setEnabled(True)

        if home is True:
            await self.home()

    async def home(
        self,
        home_telescope: bool = True,
        home_km: bool = True,
        home_focuser: bool = False,
        home_fibsel: bool = False,
    ):
        """Initialises and homes the telescope.

        Parameters
        ---------
        home_telescope
            Homes the telescope. Defaults to :obj:`True`.
        home_km
            Homes the K-mirror, if present. Defaults to :obj:`True`.
        home_focuser
            Homes the focuser. Defaults to :obj:`False`.
        home_fibsel
            Homes the fibre selector, if present. Defaults to :obj:`False`.

        """

        home_subdevices_task: asyncio.Future | None = None

        subdev_tasks = []
        if self.km is not None and home_km:
            subdev_tasks.append(self.km.home())
        if self.fibsel is not None and home_fibsel:
            subdev_tasks.append(self.fibsel.home())
        if self.focuser is not None and home_focuser:
            subdev_tasks.append(self.focuser.home())

        home_subdevices_task = asyncio.gather(*subdev_tasks)

        if home_telescope:
            if await self.gort.enclosure.is_local():
                raise GortTelescopeError(
                    "Cannot home in local mode.",
                    error_code=ErrorCode.CANNOT_MOVE_LOCAL_MODE,
                )

            self.write_to_log("Homing telescope.", level="info")

            if not (await self.is_ready()):
                await self.pwi.commands.setConnected(True)
                await self.pwi.commands.setEnabled(True)

            await self.pwi.commands.findHome()

            # findHome does not block, so wait a reasonable amount of time.
            await asyncio.sleep(15)

            self.is_homed = True

        if home_subdevices_task is not None and not home_subdevices_task.done():
            await home_subdevices_task

    async def park(
        self,
        disable=True,
        use_pw_park=False,
        alt_az: tuple[float, float] | None = None,
        kmirror: bool = True,
        force: bool = False,
    ):
        """Parks the telescope.

        Parameters
        ----------
        disable
            Disables the axes after reaching the park position.
        use_pw_park
            Uses the internal park position stored in the PlaneWave mount.
        alt_az
            A tuple with the alt and az position at which to park the telescope.
            If not provided, defaults to the ``park`` named position.
        kmirror
            Whether to park the mirror at 90 degrees.
        force
            Moves the telescope even if the mode is local.

        """

        if await self.gort.enclosure.is_local():
            raise GortTelescopeError(
                "Cannot home in local mode.",
                error_code=ErrorCode.CANNOT_MOVE_LOCAL_MODE,
            )

        await self.initialise()

        kmirror_task: asyncio.Task | None = None
        if kmirror and self.km:
            self.write_to_log("Parking k-mirror.", level="info")
            kmirror_task = asyncio.create_task(self.km.park())

        if use_pw_park:
            self.write_to_log("Parking telescope to PW default position.", level="info")
            await self.pwi.commands.park()
        elif alt_az is not None:
            self.write_to_log(f"Parking telescope to alt={alt_az[0]}, az={alt_az[1]}.")
            await self.goto_coordinates(
                alt=alt_az[0],
                az=alt_az[1],
                kmirror=False,
                altaz_tracking=False,
                force=force,
            )
        else:
            coords = self.gort.config["telescopes"]["named_positions"]["park"]["all"]
            alt = coords["alt"]
            az = coords["az"]
            self.write_to_log(f"Parking telescope to alt={alt}, az={az}.", level="info")
            await self.goto_coordinates(
                alt=alt,
                az=az,
                kmirror=False,
                altaz_tracking=False,
                force=force,
            )

        if disable:
            self.write_to_log("Disabling telescope.")
            await self.pwi.commands.setEnabled(False)

        if kmirror_task:
            await kmirror_task

        self.is_homed = False

    async def stop(self):
        """Stops the mount."""

        self.write_to_log("Stopping the mount.", "warning")
        await self.actor.commands.stop()

    async def goto_coordinates(
        self,
        ra: float | None = None,
        dec: float | None = None,
        pa: float = 0.0,
        alt: float | None = None,
        az: float | None = None,
        kmirror: bool = True,
        kmirror_kwargs: dict = {},
        altaz_tracking: bool = False,
        force: bool = False,
        retry: bool = True,
    ):
        """Moves the telescope to a given RA/Dec or Alt/Az.

        Parameters
        ----------
        ra
            Right ascension coordinates to move to, in degrees.
        dec
            Declination coordinates to move to, in degrees.
        pa
            Position angle of the IFU. Defaults to PA=0.
        alt
            Altitude coordinates to move to, in degrees.
        az
            Azimuth coordinates to move to, in degrees.
        kmirror
            Whether to move the k-mirror into position. Only when
            the coordinates provided are RA/Dec.
        kmirror_kwargs
            Dictionary of keyword arguments to pass to :obj:`.KMirror.slew`.
        altaz_tracking
            If :obj:`True`, starts tracking after moving to alt/az coordinates.
            By default the PWI won't track with those coordinates.
        force
            Move the telescopes even if mode is local.
        retry
            Retry once if the coordinates are not reached.

        """

        if not force and (await self.gort.enclosure.is_local()):
            self.write_to_log("Checking if enclosure is in local mode.")
            raise GortTelescopeError(
                "Cannot move telescope in local mode.",
                error_code=ErrorCode.CANNOT_MOVE_LOCAL_MODE,
            )

        kmirror_task: asyncio.Task | None = None
        if kmirror and self.km and ra is not None and dec is not None:
            kmirror_task = asyncio.create_task(
                self.km.slew(
                    ra,
                    dec,
                    offset_angle=pa,
                    **kmirror_kwargs,
                )
            )

        # Commanded and reported coordinates. To be used to check if we reached
        # the correct position.
        commanded: tuple[float, float]
        reported: tuple[float, float]

        await self.initialise()

        if not self.is_homed:
            self.write_to_log("Telescope is not homed. Homing now.", "warning")
            await self.home()

        if ra is not None and dec is not None:
            is_radec = ra is not None and dec is not None and not alt and not az
            assert is_radec, "Invalid input parameters"

            self.write_to_log(f"Moving to ra={ra:.6f} dec={dec:.6f}.", level="info")

            ra = float(numpy.clip(ra, 0, 360))
            dec = float(numpy.clip(dec, -90, 90))

            await self.pwi.commands.gotoRaDecJ2000(
                ra / 15.0,
                dec,
                timeout=self.timeouts["slew"],
            )

            # Check the position the PWI reports.
            status = await self.status()
            ra_status = status["ra_j2000_hours"] * 15
            dec_status = status["dec_j2000_degs"]

            commanded = (ra, dec)
            reported = (ra_status, dec_status)

        elif alt is not None and az is not None:
            is_altaz = alt is not None and az is not None and not ra and not dec
            assert is_altaz, "Invalid input parameters"

            self.write_to_log(f"Moving to alt={alt:.6f} az={az:.6f}.", level="info")
            await self.pwi.commands.gotoAltAzJ2000(
                alt,
                az,
                timeout=self.timeouts["slew"],
            )

            # Check the position the PWI reports.
            status = await self.status()
            az_status = status["azimuth_degs"]
            alt_status = status["altitude_degs"]

            commanded = (az, alt)
            reported = (az_status, alt_status)

        else:
            raise GortTelescopeError("Invalid coordinates.")

        # Check if we reached the position. If not retry once or fail.
        separation = angular_separation(*commanded, *reported)
        if separation > 0.5:
            if retry:
                self.write_to_log(
                    "Telescope failed to reach the desired position. Retrying",
                    "warning",
                )
                # Need to make sure the k-mirror is not moving before re-trying.
                if kmirror_task is not None and not kmirror_task.done():
                    await kmirror_task

                await asyncio.sleep(3)

                return await self.goto_coordinates(
                    ra=ra,
                    dec=dec,
                    alt=alt,
                    az=az,
                    pa=pa,
                    kmirror=kmirror,
                    kmirror_kwargs=kmirror_kwargs,
                    altaz_tracking=altaz_tracking,
                    force=force,
                    retry=False,
                )
            else:
                await self.actor.commands.setEnabled(False)
                raise GortTelescopeError(
                    "Telescope failed to reach desired position. "
                    "The axes have been disabled for safety. "
                    "Try re-homing the telescope.",
                    error_code=ErrorCode.FAILED_REACHING_COMMANDED_POSITION,
                )

        if alt is not None and az is not None and altaz_tracking:
            await self.pwi.commands.setTracking(enable=True)

        if kmirror_task is not None:
            await kmirror_task

    async def goto_named_position(
        self,
        name: str,
        altaz_tracking: bool = False,
        force: bool = False,
    ):
        """Sends the telescope to a named position.

        Parameters
        ----------
        name
            The name of the position, e.g., ``'zenith'``.
        altaz_tracking
            Whether to start tracking after reaching the position, if the
            coordinates are alt/az.
        force
            Move the telescope even in local enclosure mode.

        """

        if not force and (await self.gort.enclosure.is_local()):
            raise GortTelescopeError(
                "Cannot move telescope in local mode.",
                error_code=ErrorCode.CANNOT_MOVE_LOCAL_MODE,
            )

        if name not in self.config["named_positions"]:
            raise GortTelescopeError(
                f"Invalid named position {name!r}.",
                error_code=ErrorCode.INVALID_TELESCOPE_POSITION,
            )

        position_data = self.config["named_positions"][name]

        if self.name in position_data:
            coords = position_data[self.name]
        elif "all" in position_data:
            coords = position_data["all"]
        else:
            raise GortTelescopeError(
                "Cannot find position data.",
                error_code=ErrorCode.INVALID_TELESCOPE_POSITION,
            )

        if "alt" in coords and "az" in coords:
            coro = self.goto_coordinates(
                alt=coords["alt"],
                az=coords["az"],
                altaz_tracking=altaz_tracking,
                force=force,
            )
        elif "ra" in coords and "dec" in coords:
            coro = self.goto_coordinates(
                ra=coords["ra"],
                dec=coords["dec"],
                force=force,
            )
        else:
            raise GortTelescopeError(
                "No ra/dec or alt/az coordinates found.",
                error_code=ErrorCode.INVALID_TELESCOPE_POSITION,
            )

        await coro

    async def offset(
        self,
        ra: float | None = None,
        dec: float | None = None,
        axis0: float | None = None,
        axis1: float | None = None,
    ):
        """Apply an offset to the telescope axes.

        Parameters
        ----------
        ra
            Offset in RA, in arcsec.
        dec
            Offset in Dec, in arcsec.
        axis0
            Offset in axis 0, in arcsec.
        axis1
            Offset in axis 1, in arcsec.

        """

        if any([ra, dec]) and any([axis0, axis1]):
            raise GortTelescopeError(
                "Cannot offset in ra/dec and axis0/axis1 at the same time."
            )

        kwargs = {}
        if any([ra, dec]):
            if ra is not None:
                kwargs["ra_add_arcsec"] = ra
            if dec is not None:
                kwargs["dec_add_arcsec"] = dec
            self.write_to_log(f"Offsetting RA={ra:.3f}, Dec={dec:.3f}.")

        elif any([axis0, axis1]):
            if axis0 is not None:
                kwargs["axis0_add_arcsec"] = axis0
            if axis1 is not None:
                kwargs["axis1_add_arcsec"] = axis1
            self.write_to_log(f"Offsetting axis0={axis0:.3f}, axis1={axis1:.3f}.")

        else:
            # This should not happen.
            raise GortTelescopeError("No offsets provided.")

        await self.actor.commands.offset(**kwargs)

        self.write_to_log("Offset complete.")

        return True


class TelescopeSet(GortDeviceSet[Telescope]):
    """A representation of a set of telescopes."""

    __DEVICE_CLASS__ = Telescope
    __DEPLOYMENTS__ = [
        "lvmpwi-sci",
        "lvmpwi-spec",
        "lvmpwi-skye",
        "lvmpwi-skyw",
        "lvmtan",
    ]

    def __init__(self, gort: GortClient, data: dict[str, dict]):
        super().__init__(gort, data)

        self.guiders = self.gort.guiders

    async def initialise(self):
        """Initialise all telescopes."""

        await asyncio.gather(*[tel.initialise() for tel in self.values()])

    async def restart(self):
        """Restarts the ``lvmpwi`` and ``lvmtan`` deployments and re-homes."""

        result = await super().restart()

        if result is False:
            self.write_to_log(
                "Some deployments failed to restart. Not homing devices.",
                "error",
            )
            return

        self.write_to_log("Waiting 10 seconds for devices to reconnect.", "info")
        await asyncio.sleep(10)

        self.write_to_log("Homing telescope a restart.")
        await self.home(home_kms=True, home_focusers=True, home_fibsel=True)

    async def restart_lvmtan(self):
        """Restarts and rehomes Twice-As-Nice controller.

        After the actor has been restarted the K-mirrors, focuser, and fibre selector
        are rehomed. The focuser positions are preserved.

        """

        self.write_to_log("Restarting deployment lvmtan and waiting 25 s.", "info")
        await kubernetes_restart_deployment("lvmtan")
        await asyncio.sleep(25)

        await self.home(
            home_telescopes=False,
            home_kms=True,
            home_focusers=True,
            home_fibsel=True,
        )

    async def home(
        self,
        home_telescopes: bool = True,
        home_kms: bool = True,
        home_focusers: bool = False,
        home_fibsel: bool = False,
    ):
        """Initialises and homes the telescope.

        Parameters
        ---------
        home_telescopes
            Homes the telescopes. Defaults to :obj:`True`.
        home_kms
            Homes the K-mirrors, if present. Defaults to :obj:`True`.
        home_focusers
            Homes the focusers. Defaults to :obj:`False`.
        home_fibsel
            Homes the fibre selector, if present. Defaults to :obj:`False`.

        """

        self.write_to_log("Rehoming all telescopes.", "info")

        await self.call_device_method(
            Telescope.home,
            home_telescope=home_telescopes,
            home_km=home_kms,
            home_focuser=home_focusers,
            home_fibsel=home_fibsel,
        )

    async def park(
        self,
        disable=True,
        use_pw_park=False,
        alt_az: tuple[float, float] | None = None,
        kmirror=True,
        force=False,
    ):
        """Parks the telescopes.

        Parameters
        ----------
        disable
            Disables the axes after reaching the park position.
        use_pw_park
            Uses the internal park position stored in the PlaneWave mounts.
        alt_az
            A tuple with the alt and az position at which to park the telescopes.
            If not provided, defaults to the ``park`` named position.
        kmirror
            Whether to park the mirrors at 90 degrees.
        force
            Moves the telescopes even if the mode is local.

        """

        await self.call_device_method(
            Telescope.park,
            disable=disable,
            use_pw_park=use_pw_park,
            alt_az=alt_az,
            kmirror=kmirror,
            force=force,
        )

    async def goto_coordinates_all(
        self,
        ra: float | None = None,
        dec: float | None = None,
        alt: float | None = None,
        az: float | None = None,
        kmirror: bool = True,
        altaz_tracking: bool = False,
        force: bool = False,
    ):
        """Moves all the telescopes to a given RA/Dec or Alt/Az.

        Parameters
        ----------
        ra
            Right ascension coordinates to move to.
        dec
            Declination coordinates to move to.
        alt
            Altitude coordinates to move to.
        az
            Azimuth coordinates to move to.
        kmirror
            Whether to move the k-mirror into position. Only when
            the coordinates provided are RA/Dec.
        altaz_tracking
            If :obj:`True`, starts tracking after moving to alt/az coordinates.
            By defaul the PWI won't track with those coordinates.
        force
            Move the telescopes even if mode is local.

        """

        await self.call_device_method(
            Telescope.goto_coordinates,
            ra=ra,
            dec=dec,
            alt=alt,
            az=az,
            kmirror=kmirror,
            altaz_tracking=altaz_tracking,
            force=force,
        )

    async def goto_named_position(
        self,
        name: str,
        altaz_tracking: bool = False,
        force: bool = False,
    ):
        """Sends the telescopes to a named position.

        Parameters
        ----------
        name
            The name of the position, e.g., ``'zenith'``.
        altaz_tracking
            Whether to start tracking after reaching the position, if the
            coordinates are alt/az.
        force
            Move the telescopes even in local enclosure mode.

        """

        await self._check_local(force)

        if name not in self.gort.config["telescopes"]["named_positions"]:
            raise GortTelescopeError(
                f"Invalid named position {name!r}.",
                error_code=ErrorCode.INVALID_TELESCOPE_POSITION,
            )

        await self.call_device_method(
            Telescope.goto_named_position,
            name=name,
            altaz_tracking=altaz_tracking,
            force=force,
        )

    async def stop(self):
        """Stops all the mounts."""

        await self.call_device_method(Telescope.stop)

    async def goto(
        self,
        sci: tuple[float, float] | tuple[float, float, float] | None = None,
        spec: tuple[float, float] | None = None,
        skye: tuple[float, float] | None = None,
        skyw: tuple[float, float] | None = None,
        sci_km_stop_degs_before: float = 0.0,
        force: bool = False,
    ):
        """Sends each telescope to a different position.

        Parameters
        ----------
        sci
            The RA and Dec where to slew the science telescope. A third value
            can be provided for the PA. Note that in this case a -1 factor
            is applied before sending it to the K-mirror.
        spec
            The RA and Dec where to slew the spectrophotometric telescope.
        skye
            The RA and Dec where to slew the skyE telescope.
        skyw
            The RA and Dec where to slew the skyW telescope.
        sci_km_stop_degs_before
            The number of degrees before the desired position where to send
            the science k-mirror. Useful if we want to be sure that positive
            offsets will be applied without backlash.

        """

        await self._check_local(force)

        jobs = []

        if sci is not None:
            kmirror_kwargs = {"stop_degs_before": sci_km_stop_degs_before}
            if len(sci) == 2:
                jobs.append(
                    self["sci"].goto_coordinates(
                        ra=sci[0],
                        dec=sci[1],
                        kmirror_kwargs=kmirror_kwargs,
                    )
                )
            else:
                jobs.append(
                    self["sci"].goto_coordinates(
                        ra=sci[0],
                        dec=sci[1],
                        pa=-sci[2],  # Note the -1 here
                        kmirror_kwargs=kmirror_kwargs,
                    )
                )

        if spec is not None:
            jobs.append(self["spec"].goto_coordinates(ra=spec[0], dec=spec[1]))

        if skye is not None:
            jobs.append(self["skye"].goto_coordinates(ra=skye[0], dec=skye[1]))

        if skyw is not None:
            jobs.append(self["skyw"].goto_coordinates(ra=skyw[0], dec=skyw[1]))

        if len(jobs) == 0:
            return

        await asyncio.gather(*jobs)

    async def _check_local(self, force: bool = False):
        """Checks if the telescope is in local mode and raises an error."""

        is_local = await self.gort.enclosure.is_local()
        if is_local and not force:
            raise GortTelescopeError(
                "Cannot move telescopes in local mode.",
                error_code=ErrorCode.CANNOT_MOVE_LOCAL_MODE,
            )
