#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-11-05
# @Filename: troubleshooter.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from . import recipes


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


@dataclass
class TroubleModel:
    """Base model for describing a problem to troubleshoot."""

    error:

class Troubleshooter:
    """Handles troubleshooting for the Overwatcher."""

    def __init__(self, overwatcher: Overwatcher):
        self.overwatcher = overwatcher

        self.recipes = sorted(
            [Recipe(self) for Recipe in recipes.TroubleshooterRecipe.__subclasses__()],
            key=lambda x: x.priority if x.priority is not None else 1000,
        )

    def run(self):
        pass
