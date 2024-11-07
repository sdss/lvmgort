#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-11-07
# @Filename: test_overwatcher.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from gort.overwatcher.actor.actor import OverwatcherActor


async def test_overwatcher_actor(overwatcher_actor: OverwatcherActor):
    assert overwatcher_actor.is_connected
