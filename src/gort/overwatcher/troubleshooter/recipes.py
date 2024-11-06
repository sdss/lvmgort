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
    from .troubleshooter import TroubleModel, Troubleshooter


__all__ = ["TroubleshooterRecipe"]


class TroubleshooterRecipe(metaclass=abc.ABCMeta):
    """Base class for troubleshooter recipes."""

    priority: int | None = None

    def __init__(self, troubleshooter: Troubleshooter):
        self.troubleshooter = troubleshooter
        self.overwatcher = troubleshooter.overwatcher

    @abc.abstractmethod
    def match(self, error_model: TroubleModel) -> bool:
        """Returns True if the recipe can handle the error."""

        pass

    @abc.abstractmethod
    async def handle(self, error_model: TroubleModel) -> bool:
        """Runs the recipe to handle the error."""

        pass
