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

from httpx import TimeoutException
from pydantic import BaseModel, Field

from lvmopstools.utils import Trigger

from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.tools import decap, ensure_period, get_lvmapi_route


if TYPE_CHECKING:
    pass


__all__ = ["AlertsOverwatcher", "ActiveAlert"]


class AlertsSummary(BaseModel):
    """Summary of alerts."""

    time: float = Field(default_factory=time)
    humidity_alert: bool | None = None
    dew_point_alert: bool | None = None
    wind_alert: bool | None = None
    rain: bool | None = None
    door_alert: bool | None = None
    camera_temperature_alert: bool | None = None
    camera_alerts: dict[str, bool] | None = None
    e_stops: bool | None = None
    o2_alert: bool | None = None
    o2_room_alerts: dict[str, bool] | None = None
    heater_alert: bool | None = None
    heater_camera_alerts: dict[str, bool] | None = None
    engineering_override: bool = False


class ConnectivityStatus:
    """Status of the connectivity."""

    def __init__(self):
        self.lco = Trigger(n=3)
        self.internet = Trigger(n=3)


class ActiveAlert(enum.Flag):
    """Flags for active alerts."""

    HUMIDITY = enum.auto()
    DEW_POINT = enum.auto()
    WIND = enum.auto()
    RAIN = enum.auto()
    DOOR = enum.auto()
    CAMERA_TEMPERATURE = enum.auto()
    O2 = enum.auto()
    E_STOPS = enum.auto()
    ALERTS_DATA_UNAVAILABLE = enum.auto()
    DISCONNECTED = enum.auto()
    DOME_LOCKED = enum.auto()
    IDLE = enum.auto()
    ENGINEERING_OVERRIDE = enum.auto()
    UNKNOWN = enum.auto()

    ALWAYS_CLOSE = HUMIDITY | DEW_POINT | WIND | RAIN
    NO_CLOSE = DOOR | E_STOPS | ENGINEERING_OVERRIDE


ACTIVE_ALARM_DESCRIPTIONS: dict[ActiveAlert, str] = {
    ActiveAlert.HUMIDITY: "Humidity is above the threshold",
    ActiveAlert.DEW_POINT: "Ambient temperature is below dew point temperature",
    ActiveAlert.WIND: "Wind speed is above the threshold",
    ActiveAlert.RAIN: "Rain detected",
    ActiveAlert.DOOR: "Enclosure door is open",
    ActiveAlert.CAMERA_TEMPERATURE: "Spec camera temperature is above the threshold",
    ActiveAlert.O2: "O2 levels are below the safe range",
    ActiveAlert.E_STOPS: "Emergency stops have been triggered",
    ActiveAlert.ALERTS_DATA_UNAVAILABLE: "Alerts data is unavailable",
    ActiveAlert.DISCONNECTED: "Connectivity lost",
    ActiveAlert.DOME_LOCKED: "Dome is locked",
    ActiveAlert.IDLE: "Overwatcher has been idle for too long",
    ActiveAlert.ENGINEERING_OVERRIDE: "Engineering mode is enabled",
    ActiveAlert.UNKNOWN: "Unknown alert",
}


class AlertsMonitorTask(OverwatcherModuleTask["AlertsOverwatcher"]):
    """Monitors the alerts state."""

    name = "alerts_monitor"
    keep_alive = True
    restart_on_error = True

    INTERVAL: float = 30

    async def task(self):
        """Updates the alerts data."""

        SLACK_CHANNELS = self.config["overwatcher.slack.notifications_channels"]
        MAX_TIME = self.config["overwatcher.alerts.max_time_without_alerts_data"]

        while True:
            try:
                await self.update_alerts()

            except Exception as err:
                self.log.error(f"Failed retriving alerts data: {decap(err)}")

                if (
                    self.module.alerts_data_unavailable is False
                    and time() - self.module.last_updated > MAX_TIME
                ):
                    await self.module.notify_unavailable(err)

            else:
                # Send a resolution message if needed.
                if self.module.alerts_data_unavailable:
                    await self.module.notify(
                        "@here [RESOLVED]: Alerts data is now available.",
                        level="info",
                        slack_channels=[*SLACK_CHANNELS, "lvm-alerts"],
                    )

                self.module.alerts_data_unavailable = False
                self.module.last_updated = time()

            await asyncio.sleep(self.INTERVAL)

    async def update_alerts(self):
        """Processes the weather update and determines whether it is safe to observe."""

        data = await self.module.update_status()

        if data is None:
            raise ValueError("API /alerts response failed or returned no data.")

        # For some very critical alerts, we require them to be not null (null here
        # means no data was available or the API failed getting the alert data).
        if data.rain is None or data.humidity_alert is None or data.wind_alert is None:
            raise ValueError("Incomplete weather data in API /alerts response.")


class AlertsOverwatcher(OverwatcherModule):
    """Monitors alerts."""

    name = "alerts"

    tasks = [AlertsMonitorTask()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.state: AlertsSummary | None = None
        self.connectivity = ConnectivityStatus()

        # Overwatcher idle since timestamp.
        self.overwatcher_idle_since: float = 0.0

        # Last valid updated. Must include a valid response
        # from the API and weather alerts data.
        self.last_updated: float = 0.0

        # Alerts data has been unavailable for a while.
        self.alerts_data_unavailable: bool = False

    async def is_safe(self) -> tuple[bool, ActiveAlert]:
        """Determines whether it is safe to open."""

        if self.state is None:
            self.log.warning("Alerts data not available.")
            return False, ActiveAlert.UNKNOWN

        is_safe: bool = True
        active_alerts = ActiveAlert(0)

        # Keep track of how long the overwatcher has been idle.
        if self.overwatcher.state.idle and self.overwaidle_since == 0:
            self.overwaidle_since = time()
        elif not self.overwatcher.state.idle:
            self.overwaidle_since = 0

        if self.alerts_data_unavailable:
            self.log.warning("Alerts data is unavailable.")
            active_alerts |= ActiveAlert.ALERTS_DATA_UNAVAILABLE
            return False, active_alerts

        if self.alerts_data_unavailable is False and time() - self.last_updated > 600:
            # If the data is not unavailable but it has not been updated
            # in the last 10 minutes, something is wrong. We mark it as unavailable.
            # This is a redundancy since the AlertsMonitorTask should have already
            # marked it as unavailable before that.
            self.log.warning("Alerts data has not been updated in the last 10 minutes.")
            await self.notify_unavailable()
            active_alerts |= ActiveAlert.ALERTS_DATA_UNAVAILABLE
            return False, active_alerts

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

        if self.state.e_stops:
            self.log.warning("E-stops triggered.")
            active_alerts |= ActiveAlert.E_STOPS
            is_safe = False

        if self.connectivity.internet.is_set():
            self.log.warning("Internet connectivity lost.")
            active_alerts |= ActiveAlert.DISCONNECTED
            is_safe = False

        if self.connectivity.lco.is_set():
            self.log.warning("Internal LCO connectivity lost.")
            active_alerts |= ActiveAlert.DISCONNECTED
            is_safe = False

        if self.overwatcher.dome.locked:
            self.log.warning("Dome is locked.")
            active_alerts |= ActiveAlert.DOME_LOCKED
            is_safe = False

        if self.state.door_alert:
            active_alerts |= ActiveAlert.DOOR
            is_safe = False

        # These alerts are not critical but we log them.
        # TODO: maybe we do want to do something about these alerts.
        if self.state.camera_temperature_alert:
            self.log.warning("Camera temperature alert detected.")
            active_alerts |= ActiveAlert.CAMERA_TEMPERATURE

        if self.state.o2_alert:
            self.log.warning("O2 alert detected.")
            active_alerts |= ActiveAlert.O2

        if is_safe and self.overwatcher.state.enabled and self.overwatcher.state.night:
            # If it's safe to observe but we have been idle for a while, we
            # raise an alert but do not change the is_safe status.
            timeout = self.overwatcher.config["overwatcher.alerts.idle_timeout"] or 600
            if self.overwaidle_since > 0 and (time() - self.overwaidle_since) > timeout:
                await self.notify(
                    f"Overwatcher has been idle for over {timeout:.0f} s.",
                    min_time_between_repeat_notifications=300,
                )
                active_alerts |= ActiveAlert.IDLE

        # If the engineering mode is enabled, we assume it's safe.
        if self.state.engineering_override:
            self.log.warning("Engineering mode is enabled.")
            active_alerts |= ActiveAlert.ENGINEERING_OVERRIDE
            is_safe = True

        return is_safe, active_alerts

    async def notify_unavailable(self, err: Exception | str | None = None):
        """Sends a notification that alerts data is unavailable."""

        msg = "Failed to retrieve alerts data multiple times"
        if err is not None and str(err) != "":
            msg += f": {decap(str(err))}"

        await self.notify(ensure_period(msg), level="critical")

        self.alerts_data_unavailable = True

    async def update_status(self) -> AlertsSummary | None:
        """Returns the alerts report."""

        try:
            alerts_data = await get_lvmapi_route("/alerts/summary", timeout=10)
        except TimeoutException:
            self.log.warning("Timeout retrieving /alerts/summary from LVM API")
            alerts_data = {}

        summary = AlertsSummary(**alerts_data)
        self.state = summary

        # For connectivity we want to avoid one single failure to trigger an alert
        # which closes the dome. The connectivity status is a set of triggers that
        # need several settings to be activated.
        try:
            connectivity_data = await get_lvmapi_route(
                "/alerts/connectivity",
                timeout=10,
            )
        except TimeoutException:
            self.log.warning("Timeout retrieving /alerts/connectivity from LVM API")
            connectivity_data = {"lco": False, "internet": False}

        if not connectivity_data["lco"]:
            self.connectivity.lco.set()
        else:
            self.connectivity.lco.reset()

        if not connectivity_data["internet"]:
            self.connectivity.internet.set()
        else:
            self.connectivity.internet.reset()

        return self.state
