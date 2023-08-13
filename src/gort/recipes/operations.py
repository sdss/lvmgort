#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-08-13
# @Filename: operations.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from rich.prompt import Confirm

from .base import BaseRecipe


if TYPE_CHECKING:
    pass


__all__ = ["StartupRecipe"]


class StartupRecipe(BaseRecipe):
    """Starts the telescopes, runs the calibration sequence, and opens the enclosure."""

    name = "startup"

    async def recipe(
        self,
        calibration_sequence: str | None | Literal[False] = None,
        open_enclosure: bool = True,
        confirm_open: bool = True,
        focus: bool = True,
    ):
        """Runs the startup sequence.

        Parameters
        ----------
        gort
            The `.Gort` instance to use.
        calibration_sequence
            The name of the calibration sequence to use. If `None`, uses the default
            sequence from the configuration. If `False`, skips the calibration sequence.
        open_enclosure
            Whether to open the enclosure.
        confirm_open
            If `True`, asks the user to confirm opening the enclosure.
        focus
            Whether to focus after the enclosure has open.

        """

        rconfig = self.gort.config["recipes"]["startup"]

        self.gort.log.warning("Running startup sequence.")

        await self.gort.telescopes.home(
            home_telescopes=True,
            home_kms=True,
            home_focusers=True,
            home_fibsel=True,
        )

        self.gort.log.info("Turning off all lamps.")
        await self.gort.nps.calib.all_off()

        self.gort.log.info("Taking AG darks.")
        await self.gort.guiders.take_darks()

        if calibration_sequence is not False:
            sequence = calibration_sequence or rconfig["calibration_sequence"]
            self.gort.log.info(f"Running calibration sequence {sequence!r}.")
            await self.gort.specs.calibrate(sequence)

        if open_enclosure:
            if confirm_open:
                if not Confirm.ask(
                    "Open the dome?",
                    default=False,
                    console=self.gort._console,
                ):
                    return

            self.gort.log.info("Opening the dome ...")
            await self.gort.enclosure.open()

        if open_enclosure and focus:
            self.gort.log.info("Focusing telescopes.")
            await self.gort.guiders.focus()

        self.gort.log.info("The startup recipe has completed.")
