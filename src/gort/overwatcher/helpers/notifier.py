#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-08-24
# @Filename: notifier.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import datetime
import json
import logging
from traceback import format_exception

from typing import TYPE_CHECKING, Any, Literal, Protocol

import httpx

from sdsstools import Configuration
from sdsstools.utils import GatheringTaskGroup

from gort.core import LogNamespace
from gort.tools import insert_to_database


if TYPE_CHECKING:
    from gort.gort import Gort


NotificationLevel = Literal["debug", "info", "warning", "error", "critical"]
Channels = Literal["slack", "email"]

GORT_ICON_URL = "https://github.com/sdss/lvmgort/blob/main/docs/sphinx/_static/gort_logo_slack.png?raw=true"


class OverwatcherProtocol(Protocol):
    gort: Gort
    log: LogNamespace
    config: Configuration


class NotifierMixIn(OverwatcherProtocol):
    """A mix-in class for :obj:`.Overwatcher `that adds notification capabilities."""

    async def write_to_slack(
        self,
        text: str,
        channel: str | None = None,
        as_overwatcher: bool = True,
        mentions: list[str] = [],
    ):
        """Writes a message to Slack."""

        username = "Overwatcher" if as_overwatcher else None
        icon_url = GORT_ICON_URL if as_overwatcher else None

        host, port = self.config["services.lvmapi"].values()
        channel = channel or self.config["overwatcher.slack.notifications_channel"]

        try:
            async with httpx.AsyncClient(
                base_url=f"http://{host}:{port}",
                follow_redirects=True,
            ) as client:
                response = await client.post(
                    "/slack/message",
                    json={
                        "text": text,
                        "username": username,
                        "icon_url": icon_url,
                        "mentions": mentions,
                        "channel": channel,
                    },
                )

                if response.status_code != 200:
                    raise ValueError(response.text)
        except Exception as err:
            self.log.error(f"Failed to send message to Slack: {err}")

    async def notify(
        self,
        message: str | None = None,
        level: NotificationLevel | None = None,
        error: str | Exception | None = None,
        with_traceback: bool = True,
        channels: Channels | list[Channels] | None = None,
        slack_channels: list[str] | None = None,
        database: bool = True,
        log: bool = True,
        payload: dict[str, Any] = {},
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
        channels
            A list of channels to send the notification to. Available channels
            are 'slack' and 'email'. If not provided, the channels are determined
            based on the level.
        slack_channels
            The Slack channels to which to send the notification. By default
            ``lvm-alerts`` is notified for ``error`` or ``critical`` messages,
            and ``lvm-overwatcher`` for anything lower.
        database
            Whether to record the notification in the database.
        log
            Whether to record the notification in the log.
        payload
            Additional notification payload as a JSON-like dictionary. Only
            saved to the database notifications table.

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

        if channels is None:
            if level in ["critical"]:
                channels = ["slack", "email"]
            else:
                channels = ["slack"]
        elif isinstance(channels, str):
            channels = [channels]

        if "slack" in channels:
            slack_config = self.config["overwatcher.slack"]
            mentions: list[str] = []
            if slack_channels is None:
                slack_channels = [slack_config["notifications_channel"]]
                if level in ["critical"]:
                    slack_channels.append(slack_config["alerts_channel"])
                if level in ["error", "critical"]:
                    mentions.append("@channel")

            async with GatheringTaskGroup() as group:
                for slack_channel in slack_channels:
                    group.create_task(
                        self.write_to_slack(
                            full_message,
                            channel=slack_channel,
                            as_overwatcher=True,
                            mentions=mentions,
                        )
                    )

        if database:
            table = self.config["services.database.tables.notifications"]

            if trace:
                payload["traceback"] = trace

            insert_to_database(
                table,
                [
                    {
                        "date": datetime.datetime.now(tz=datetime.UTC),
                        "level": level,
                        "message": message,
                        "payload": json.dumps(payload),
                        "slack": "slack" in channels,
                        "email": "email" in channels,
                    }
                ],
            )


class BasicNotifier(NotifierMixIn):
    """Basic notifier."""

    def __init__(self, gort: Gort):
        self.gort = gort
        self.config = Configuration(gort.config)
        self.log = LogNamespace(gort.log)
