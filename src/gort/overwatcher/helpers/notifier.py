#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-08-24
# @Filename: notifier.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import hashlib
import logging
from time import time
from traceback import format_exception

from typing import TYPE_CHECKING, Any, Literal, Protocol, Sequence, cast

import httpx

from sdsstools import Configuration

from gort import config
from gort.tools import LogNamespace


if TYPE_CHECKING:
    from gort.gort import Gort


NotificationLevel = Literal["debug", "info", "warning", "error", "critical"]

GORT_ICON_URL = "https://github.com/sdss/lvmgort/blob/main/docs/sphinx/_static/gort_logo_slack.png?raw=true"


class OverwatcherProtocol(Protocol):
    gort: Gort
    log: LogNamespace
    config: Configuration


class NotifierMixIn(OverwatcherProtocol):
    """A mix-in class for :obj:`.Overwatcher `that adds notification capabilities."""

    # A dictionary of notification hash and the timestamp
    # when the notification can be sent again.
    notification_history: dict[str, float] = {}

    async def notify(
        self,
        message: str | None = None,
        level: NotificationLevel | None = None,
        error: str | Exception | None = None,
        with_traceback: bool = True,
        slack: bool = True,
        slack_channels: str | Sequence[str] | None = None,
        database: bool = True,
        log: bool = True,
        payload: dict[str, Any] = {},
        as_overwatcher: bool = True,
        allow_repeat_notifications: bool = False,
        min_time_between_repeat_notifications: int = 60,
        raise_on_error: bool = False,
    ):
        """Emits a notification to Slack or email.

        The notification is logged to the GORT/Overwatcher log and to the
        database. Depending on severity and arguments, a notification is then
        issued over Slack to the appropriate channel, or over email for serious
        alerts.

        Parameters
        ----------
        message
            The message to send.
        level
            The level of the message. One of 'debug', 'info', 'warning', 'error',
            or 'critical'. If :obj:`None`, the level is set to ``error`` if
            ``error`` is provided, and to ``info`` otherwise. Critical errors
            are sent to the ``lvm-alerts`` Slack channel.
        error
            An error message or exception to include in the notification.
        with_traceback
            Whether to include the traceback in the notification. Requires
            ``error`` to be an exception object.
        slack
            Whether to send the notification to Slack.
        slack_channels
            The Slack channels to which to send the notification. By default
            ``lvm-alerts`` is notified for ``critical`` messages,
            and ``lvm-overwatcher`` for anything lower.
        database
            Whether to record the notification in the database.
        log
            Whether to record the notification in the log.
        payload
            Additional notification payload as a JSON-like dictionary. Only
            saved to the database notifications table.
        as_overwatcher
            Whether to send the message as the Overwatcher bot.
        allow_repeat_notifications
            Whether to allow the same notification to be sent multiple times.
        min_time_between_repeat_notifications
            The minimum time in seconds between repeated notifications. Ignored
            if ``allow_repeat_notifications`` is ``False``. This only affects Slack
            and email notifications. The notification is always recorded to the log.
        raise_on_error
            Whether to raise an exception if the notification fails to send. Otherwise
            the error is logged but the function does not raise.

        """

        if level is None:
            level = "error" if error is not None else "info"

        message = message or ""
        if error is not None and message == "":
            message = f" {str(error)}"

        trace: str | None = None
        if with_traceback and isinstance(error, Exception):
            trace = "".join(format_exception(type(error), error, error.__traceback__))

        full_message = message
        if trace:
            full_message += f"\n{trace}" if full_message else trace

        if log:
            log_level = logging._nameToLevel[level.upper()]
            self.log.logger.log(log_level, self.log._get_message(full_message))

        # Now create the notification actual notification by calling the API.
        # This will load it to the database. We do not emit emails for now.
        api_host, api_port = config["services"]["lvmapi"].values()

        slack_config = self.config["overwatcher.slack"]
        if slack_channels is None:
            slack_channels = cast(str, slack_config["notifications_channels"])
        elif isinstance(slack_channels, str):
            slack_channels = [slack_channels]
        else:
            slack_channels = list(slack_channels)

        # Create a notification hash to uniquely identify the notification.
        notification_hash = self.create_notification_hash(
            message=message,
            level=level,
            error=error,
            slack=slack,
            slack_channels=slack_channels,
            payload=payload,
        )

        # Do not emit Slack notification if this is a repeated notification
        next_notification_time = self.notification_history.get(notification_hash)
        if (
            not allow_repeat_notifications
            and next_notification_time
            and next_notification_time > time()
        ):
            slack = False

        try:
            async with httpx.AsyncClient(
                base_url=f"http://{api_host}:{api_port}",
                follow_redirects=True,
            ) as client:
                response = await client.post(
                    "/notifications/create",
                    json={
                        "message": full_message,
                        "level": level.upper(),
                        "payload": payload,
                        "slack": slack,
                        "slack_channels": slack_channels,
                        "email_on_critical": False,
                        "write_to_database": database,
                        "slack_extra_params": {
                            "username": "Overwatcher" if as_overwatcher else None,
                            "icon_url": GORT_ICON_URL if as_overwatcher else None,
                        },
                    },
                )

                code = response.status_code
                if code != 200:
                    raise RuntimeError(f"Failed creating notification. Code {code}.")

        except Exception as err:
            if not raise_on_error:
                self.log.error("Failed to create notification.", exc_info=err)
            else:
                raise

        next_notification_time = time() + min_time_between_repeat_notifications
        self.notification_history[notification_hash] = next_notification_time

    def create_notification_hash(
        self,
        message: str | None = None,
        level: NotificationLevel | None = None,
        error: str | Exception | None = None,
        slack: bool = True,
        slack_channels: str | Sequence[str] | None = None,
        payload: dict[str, Any] = {},
    ):
        """Creates a hash for a notification."""

        hash_elements = [
            str(message) if message else "",
            level or "",
            str(error) if error else "",
            str(slack),
            str(slack_channels) if isinstance(slack_channels, (str, list)) else "",
            str(payload) if payload else "",
        ]

        hasher = hashlib.new("md5")
        hasher.update("".join(hash_elements).encode())

        return hasher.hexdigest()


class BasicNotifier(NotifierMixIn):
    """Basic notifier."""

    def __init__(self, gort: Gort):
        self.gort = gort
        self.config = Configuration(gort.config)
        self.log = LogNamespace(gort.log)
