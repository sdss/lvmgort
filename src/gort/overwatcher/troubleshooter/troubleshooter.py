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

from gort.enums import ErrorCode
from gort.exceptions import GortError

from . import recipes


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


@dataclass
class TroubleModel:
    """Base model for describing a problem to troubleshoot."""

    error: Exception | None
    error_code: ErrorCode
    message: str | None = None
    handled: bool = False


class Troubleshooter:
    """Handles troubleshooting for the Overwatcher."""

    def __init__(self, overwatcher: Overwatcher):
        self.overwatcher = overwatcher

        self.recipes = sorted(
            [Recipe(self) for Recipe in recipes.TroubleshooterRecipe.__subclasses__()],
            key=lambda x: x.priority if x.priority is not None else 1000,
        )

    async def handle(self, error: Exception | str):
        """Handles an error and tries to troubleshoot it.

        Parameters
        ----------
        error
            The error to troubleshoot. This can be an exception (usually an instance
            of :obj:`.GortError`) or a string with the error message.

        """

        if not isinstance(error, (Exception, str)):
            raise RuntimeError("error must be an exception or a string.")

        if isinstance(error, str):
            error_model = TroubleModel(
                error=None,
                error_code=ErrorCode.UNCATEGORISED_ERROR,
                message=error,
            )
        elif isinstance(error, GortError):
            error_model = TroubleModel(
                error=error,
                error_code=error.error_code,
                message=str(error),
            )
        else:
            error_model = TroubleModel(
                error=error,
                error_code=ErrorCode.UNCATEGORISED_ERROR,
                message=str(error),
            )

        for recipe in self.recipes:
            if recipe.match(error_model):
                result = await recipe.handle(error_model)
                if result:
                    error_model.handled = True
                    break
