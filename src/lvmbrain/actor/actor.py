#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-10
# @Filename: actor.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from clu.actor import AMQPActor


__all__ = ["LVMBrainActor"]


class LVMBrainActor(AMQPActor):
    """The ``lvmbrain`` actor."""

    pass
