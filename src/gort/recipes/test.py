#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-08-22
# @Filename: test.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from .base import BaseRecipe


__all__ = ["TestRecipe"]


class TestRecipe(BaseRecipe):
    """A test recipe."""

    name = "test_recipe"

    async def recipe(self, fail: bool = False):
        """The test recipe."""

        self.gort.log.info("Running test recipe.")

        await asyncio.sleep(5)

        if fail:
            self.gort.log.error("Test recipe failed.")
            raise ValueError("Test recipe failed.")

        self.gort.log.info("Test recipe completed.")
