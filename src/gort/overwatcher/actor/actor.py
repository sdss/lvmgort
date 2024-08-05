#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-08-03
# @Filename: actor.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from clu.actor import AMQPActor
from clu.command import Command


__all__ = ["OverwatcherActor", "OverwatcherCommand"]


class OverwatcherActor(AMQPActor):
    """An actor that watches over other actors!"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.log.info("OverwatcherActor initialised.")

    async def start(self, **kwargs):
        """Starts the overwatcher and actor."""

        # await Overwatcher().run()

        return await super().start(**kwargs)


OverwatcherCommand = Command[OverwatcherActor]
