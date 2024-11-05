#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-10-27
# @Filename: alerts.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import enum
from time import time

from typing import TYPE_CHECKING

from lvmopstools.retrier import Retrier
from pydantic import BaseModel

from gort.overwatcher.core import OverwatcherModuleTask
from gort.overwatcher.overwatcher import OverwatcherModule
from gort.tools import get_lvmapi_route


if TYPE_CHECKING:
    pass


__all__ = ["AlertsOverwatcher", "ActiveAlert"]


class AlertsSummary(BaseModel):
    """Summary of alerts."""

    humidity_alert: bool | None = None
    dew_point_alert: bool | None = None
    wind_alert: bool | None = None
    rain: bool | None = None
    door_alert: bool | None = None
    camera_temperature_alert: bool | None = None
    camera_alerts: dict[str, bool] | None = None
    o2_alert: bool | None = None
    o2_room_alerts: dict[str, bool] | None = None
    heater_alert: bool | None = None
    heater_camera_alerts: dict[str, bool] | None = None


class ActiveAlert(enum.Flag):
    """Flags for active alerts."""

    HUMIDITY = enum.auto()
    DEW_POINT = enum.auto()
    WIND = enum.auto()
    RAIN = enum.auto()
    DOOR = enum.auto()
    CAMERA_TEMPERATURE = enum.auto()
    O2 = enum.auto()
    UNKNOWN = enum.auto()


class AlertsMonitorTask(OverwatcherModuleTask["AlertsOverwatcher"]):
    """Monitors the alerts state."""

    name = "alerts_monitor"
    keep_alive = True
    restart_on_error = True

    def __init__(self):
        super().__init__()

        self.last_updated: float = 0.0
        self.unavailable: bool = False

    async def task(self):
        """Updates the alerts data."""

        n_failures: int = 0

        while True:
            try:
                await self.update_alerts()
            except Exception as err:
                if self.unavailable is False:
                    self.log.error(f"Failed to get alerts data: {err!r}")
                n_failures += 1
            else:
                self.last_updated = time()
                self.unavailable = False
                n_failures = 0
            finally:
                if self.unavailable is False and n_failures >= 5:
                    self.unavailable = True
                    self.log.critical(
                        "Failed to get alerts data 5 times. "
                        "Triggering an emergency shutdown.",
                    )
                    asyncio.create_task(
                        self.overwatcher.shutdown(
                            reason="alerts data unavailable",
                            park=False,
                        )
                    )

            await asyncio.sleep(15)

    async def update_alerts(self):
        """Processes the weather update and determines whether it is safe to observe."""

        data = await self.module.get_alerts_summary()

        if data is None:
            raise ValueError("no alerts data available.")

        # For some very critical alerts, we require them to be not null (null here
        # means no data was available or the API failed getting the alert data).
        if data.rain is None or data.humidity_alert is None or data.wind_alert is None:
            raise ValueError("incomplete alerts data.")

        self.module.state = data


class AlertsOverwatcher(OverwatcherModule):
    """Monitors alerts."""

    name = "alerts"

    tasks = [AlertsMonitorTask()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.state: AlertsSummary | None = None
        self.locked_until: float = 0

    def is_safe(self) -> tuple[bool, ActiveAlert]:
        """Determines whether it is safe to open."""

        if self.state is None:
            self.log.warning("Alerts data not available. is_safe() returns False.")
            return False, ActiveAlert.UNKNOWN

        # If we have issued a previous unsafe alert, the main task will close the dome
        # and put a lock for 30 minutes to prevent the dome from opening/closing too
        # frequently if the weather is unstable.
        if self.locked_until > 0 and time() < self.locked_until:
            return False, ActiveAlert(0)

        is_safe: bool = True
        active_alerts = ActiveAlert(0)

        if self.state.rain:
            self.log.warning("Rain alert detected.")
            active_alerts |= ActiveAlert.RAIN
            is_safe = False
        if self.state.humidity_alert:
            self.log.warning("Humidity alert detected.")
            active_alerts |= ActiveAlert.HUMIDITY
            is_safe = False
        if self.state.dew_point_alert:
            self.log.warning("Dew point alert detected.")
            active_alerts |= ActiveAlert.DEW_POINT
            is_safe = False
        if self.state.wind_alert:
            self.log.warning("Wind alert detected.")
            active_alerts |= ActiveAlert.WIND
            is_safe = False

        # These alerts are not critical but we log them.
        # TODO: maybe we do want to do something about these alerts.
        if self.state.door_alert:
            self.log.warning("Door alert detected.")
            active_alerts |= ActiveAlert.DOOR
        if self.state.camera_temperature_alert:
            self.log.warning("Camera temperature alert detected.")
            active_alerts |= ActiveAlert.CAMERA_TEMPERATURE
        if self.state.o2_alert:
            self.log.warning("O2 alert detected.")
            active_alerts |= ActiveAlert.O2

        if is_safe:
            self.locked_until = 0

        return is_safe, active_alerts

    @Retrier(max_attempts=3, delay=5)
    async def get_alerts_summary(self) -> AlertsSummary:
        """Returns the alerts report."""

        data = await get_lvmapi_route("/alerts/summary")

        return AlertsSummary(**data)
