#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-08-24
# @Filename: notifier.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import logging
from traceback import format_exception

from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import httpx

from sdsstools import Configuration

from gort import config
from gort.core import LogNamespace


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

    async def notify(
        self,
        message: str | None = None,
        level: NotificationLevel | None = None,
        error: str | Exception | None = None,
        with_traceback: bool = True,
        slack_channel: str | bool | None = None,
        database: bool = True,
        log: bool = True,
        payload: dict[str, Any] = {},
        as_overwatcher: bool = True,
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
        slack_channel
            The Slack channel to which to send the notification. By default
            ``lvm-alerts`` is notified for ``critical`` messages,
            and ``lvm-overwatcher`` for anything lower. If ``False``, no
            Slack notifications are sent.
        database
            Whether to record the notification in the database.
        log
            Whether to record the notification in the log.
        payload
            Additional notification payload as a JSON-like dictionary. Only
            saved to the database notifications table.
        as_overwatcher
            Whether to send the message as the Overwatcher bot.

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
        if slack_channel is None or slack_channel is True:
            slack_channel = cast(str, slack_config["notifications_channel"])

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
                    "slack_channel": slack_channel,
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
                self.log.warning(f"Failed adding night log comment. Code {code}.")


class BasicNotifier(NotifierMixIn):
    """Basic notifier."""

    def __init__(self, gort: Gort):
        self.gort = gort
        self.config = Configuration(gort.config)
        self.log = LogNamespace(gort.log)
