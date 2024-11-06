#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-11-05
# @Filename: recipes.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import abc

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from gort.overwatcher.troubleshooter.troubleshooter import Troubleshooter


__all__ = ["TroubleshooterRecipe"]


class TroubleshooterRecipe(metaclass=abc.ABCMeta):
    """Base class for troubleshooter recipes."""

    priority: int | None = None

    def __init__(self, troubleshooter: Troubleshooter):
        self.troubleshooter = troubleshooter
        self.overwatcher = troubleshooter.overwatcher

    def match
    @abc.abstractmethod
    async def run(self, ):
        pass

class TestRecipe(TroubleshooterRecipe):
    pass
