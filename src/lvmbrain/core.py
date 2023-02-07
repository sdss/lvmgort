#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-02-07
# @Filename: core.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import uuid

from clu.client import AMQPClient

from lvmbrain.exceptions import LVMBrainNotImplemented


class LVMBrain(AMQPClient):
    """The main ``lvmbrain`` class, used to communicate with the actor system."""

    def __init__(self, host="lvm-hub.lco.cl", user: str = "guest", password="guest"):
        client_uuid = str(uuid.uuid4()).split("-")[1]

        super().__init__(
            f"lvmbrain-client-{client_uuid}",
            host=host,
            user=user,
            password=password,
        )

    def connected(self):
        """Returns `True` if the client is connected."""

        return self.connection and self.connection.connection is not None

    async def acquire_and_expose(
        self,
        ra: float,
        dec: float,
        exposure_time=900.0,
        n_exposures=1,
        pa=0.0,
        standards: list[tuple[float, float]] | None = None,
        standards_exposure_time: float | list[float] | None = None,
        sky_positions: list[tuple[float, float]] | None = None,
        guide_exposure_time: float | None = None,
        guide_rms=1.0,
        acquisition_timeout=120,
    ):
        """Commands the four LVM telescope to point and expose.

        Parameters
        ----------
        ra
            The right ascension of the science telescope / centre of the
            science bundle.
        dec
            The declination of the science telescope.
        exposure_time
            The science exposure time, in seconds.
        n_exposures
            Number of science exposures to obtain.
        pa
            The position angle of the science bundle.
        standards
            A list of ``(ra, dec)`` tuples with the positions of the standards
            to observe during the exposure. If `None`, the scheduler will be
            queried to provide a list of appropriate standards for the science
            field.
        standards_exposure_time
            The exposure time for each standard. If the value is less than 1, it
            is understood as the fraction of ``exposure_time`` to spend on each
            standard. If a list, it must have the same size as ``standards`` and
            specify the exposure time for each standard. If `None`, an equal
            amount of time will be spent on each standard position.
        sky_positions
            A list of ``(ra, dec)`` tuples with the positions of the two sky fields
            to observe during the exposure. If `None`, the scheduler will be queried
            to provide a list of appropriate sky positions.
        guide_exposure_time
            The exposure time for each guider frame. If `None`, the exposure time
            will be selected dynamically.
        guide_rms
            The target guide RMS to reach before starting the science exposure.
        acquisition_timeout
            The maximum amount of time allowed for the acquisition.

        Returns
        -------
        result
            `True` if the field has been successfully observed.

        Raises
        ------
        LVMBrainError
            If an error occurs during acquisition or exposure.
        LVMBrainTimeout
            If a timeout occurs.

        """

        raise LVMBrainNotImplemented()
