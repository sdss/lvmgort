#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-12-20
# @Filename: cleanup.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from gort import Gort


async def cleanup():
    gort = await Gort(verbosity="debug").init()

    await gort.cleanup()


if __name__ == "__main__":
    asyncio.run(cleanup())
