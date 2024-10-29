#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-08-03
# @Filename: actor.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import pathlib

from clu.actor import AMQPActor
from clu.command import Command

import gort
from gort.overwatcher.overwatcher import Overwatcher

from .commands import overwatcher_cli


__all__ = ["OverwatcherActor", "OverwatcherCommand"]


class OverwatcherActor(AMQPActor):
    """An actor that watches over other actors!"""

    parser = overwatcher_cli

    def __init__(self, *args, dry_run: bool = False, **kwargs):
        gort_root = pathlib.Path(gort.__file__).parent
        schema = gort_root / "etc" / "actor_schema.json"

        super().__init__(*args, schema=schema, version=gort.__version__, **kwargs)

        self.overwatcher = Overwatcher(dry_run=dry_run)

        self.log.info("OverwatcherActor initialised.")

    async def start(self, **kwargs):
        """Starts the overwatcher and actor."""

        await self.overwatcher.run()

        return await super().start(**kwargs)


OverwatcherCommand = Command[OverwatcherActor]
