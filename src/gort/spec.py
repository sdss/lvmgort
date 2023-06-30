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
from contextlib import suppress

from typing import TYPE_CHECKING

from gort import config
from gort.exceptions import GortSpecError
from gort.gort import GortDevice, GortDeviceSet
from gort.tools import move_mask_interval, tqdm_timer


if TYPE_CHECKING:
    from gort.core import ActorReply
    from gort.gort import GortClient


READOUT_TIME = 56


class Spectrograph(GortDevice):
    """Class representing an LVM spectrograph functionality."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

    async def status(self):
        """Retrieves the status of the telescope."""

        reply: ActorReply = await self.actor.commands.status()
        return reply.flatten()

    async def is_idle(self):
        """Returns `True` if the spectrograph is idle and ready to expose."""

        status = await self.status()
        try:
            if "IDLE" in status["status"]["status_names"]:
                return True
        except Exception:
            pass

        return False

    async def expose(self, **kwargs):
        """Exposes the spectrograph."""

        if not (await self.is_idle()):
            raise GortSpecError("Spectrographs is not idle. Cannot expose.")

        self.write_to_log(f"Exposing spectrograph {self.name}.")

        await self.actor.commands.expose(**kwargs)


class SpectrographSet(GortDeviceSet[Spectrograph]):
    """A set of LVM spectrographs."""

    __DEVICE_CLASS__ = Spectrograph

    def get_seqno(self):
        """Returns the next exposure sequence number."""

        next_exposure_number_path = config["specs"]["nextExposureNumber"]
        with open(next_exposure_number_path, "r") as fd:
            data = fd.read().strip()
            seqno = int(data) if data != "" else 1

        return seqno

    async def are_idle(self):
        """Returns `True` if all the spectrographs are idle and ready to expose."""

        result = await asyncio.gather(*[spec.is_idle() for spec in self.values()])

        return all(result)

    async def expose(
        self,
        tile_data: dict | None = None,
        show_progress: bool = False,
        **kwargs,
    ):
        """Exposes the spectrographs."""

        if not (await self.are_idle()):
            raise GortSpecError("Spectrographs are not idle. Cannot expose.")

        count: int = kwargs.pop("count", 1)
        exposure_time: float = kwargs.pop("exposure_time", 10)

        exp_nos = []

        for _ in range(count):
            seqno = self.get_seqno()
            self.write_to_log(f"Taking spectrograph exposure {seqno}.", level="info")

            await self.reset()

            if tile_data is not None:
                header = json.dumps(tile_data)
            else:
                header = None

            if show_progress:
                timer = tqdm_timer(exposure_time + READOUT_TIME)
            else:
                timer = None

            try:
                await self._send_command_all(
                    "expose",
                    exposure_time=exposure_time,
                    seqno=seqno,
                    header=header,
                    **kwargs,
                )
                exp_nos.append(seqno)
            except Exception as err:
                with suppress(asyncio.CancelledError):
                    if timer:
                        await timer

                raise GortSpecError(f"Exposure failed with error {err}")

        return exp_nos

    async def reset(self):
        """Reset the spectrographs."""

        await self._send_command_all("reset")

    async def calibrate(self, sequence: str = "normal", park_after: bool = True):
        """Runs the calibration sequence.

        Parameters
        ----------
        sequence
            The calibration sequence to execute.
        park_after
            Park the telescopes after a successful calibration sequence.

        """

        # TODO: add some checks. Confirm HDs are open, specs connected, etc.

        cal_config = config["specs"]["calibration"]

        if sequence not in cal_config["sequences"]:
            raise GortSpecError(f"Unknown sequence {sequence!r}.")
        sequence_config = cal_config["sequences"][sequence]

        self.write_to_log("Moving telescopes to position.", level="info")
        await self.gort.telescopes.goto_named_position(cal_config["position"])

        calib_nps = self.gort.nps[cal_config["lamps_nps"]]
        lamps_config = sequence_config.get("lamps", {})

        fibsel_task: asyncio.Task | None = None

        # Turn off all lamps.
        self.write_to_log("Checking that all lamps are off.", level="info")
        await calib_nps.all_off()

        self.write_to_log(f"Running calibration sequence {sequence!r}.", level="info")

        try:
            for lamp in lamps_config:
                warmup = lamps_config[lamp]["warmup"]

                self.write_to_log(f"Warming up lamp {lamp} ({warmup} s).", level="info")
                await calib_nps.on(lamp)
                await asyncio.sleep(warmup)

                for exp_time in lamps_config[lamp]["exposure_times"]:
                    flavour = lamps_config[lamp]["flavour"]

                    # Check if we are spinning the fibre selector and,
                    # if so, launch the task.
                    fibsel = lamps_config[lamp].get("fibsel", None)
                    if fibsel:
                        initial_position = fibsel.get("initial_position", None)
                        positions = fibsel.get("positions", "P1-*")
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
                    )

                    if fibsel_task:
                        await fibsel_task

                self.write_to_log(f"Turning off {lamp}.")
                await calib_nps.off(lamp)

            if "bias" in sequence_config:
                self.write_to_log("Taking biases.", level="info")
                nbias = sequence_config["biases"].get("count", 1)
                for _ in range(nbias):
                    await self.gort.specs.expose(flavour="bias")

            if "darks" in sequence_config:
                self.write_to_log("Taking darks.", level="info")
                ndarks = sequence_config["darks"].get("count")
                for _ in range(ndarks):
                    await self.gort.specs.expose(
                        flavour="dark",
                        exposure_time=sequence_config["darks"]["exposure_time"],
                    )

            if park_after:
                await self.gort.telescopes.park()

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
