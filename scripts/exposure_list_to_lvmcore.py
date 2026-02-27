#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2026-02-27
# @Filename: exposure_list_to_lvmcore.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import os
import pathlib

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn

from sdsstools.time import get_sjd

from gort.tools import get_exposure_list


async def exposure_list_to_lvmcore(overwrite: bool = False):
    """Populates ``lvmcore`` with ``exposure_list`` files for all MJDs."""

    lvmcore_dir = os.environ.get("LVMCORE_DIR", None)
    if not lvmcore_dir:
        raise RuntimeError("LVMCORE_DIR environment variable is not set.")

    lvmcore_dir = pathlib.Path(lvmcore_dir)
    if not lvmcore_dir.exists():
        raise RuntimeError(f"LVMCORE_DIR {lvmcore_dir} does not exist.")

    exp_list_dir = lvmcore_dir / "exposure_list"
    exp_list_dir.mkdir(exist_ok=True)

    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        expand=True,
        auto_refresh=True,
    )

    mjd0 = 60007
    mjd1 = get_sjd("LCO")

    task = progress.add_task(f"Processing MJD {mjd0}", total=(mjd1 - mjd0 + 1))

    with progress:
        for mjd in range(mjd0, mjd1 + 1):
            exp_list_file = exp_list_dir / f"exposure_list_{mjd}.parquet"
            if exp_list_file.exists() and not overwrite:
                progress.update(task, advance=1)
                continue

            progress.update(task, description=f"Processing MJD {mjd}")

            try:
                df = await get_exposure_list(mjd)
                df.write_parquet(exp_list_file)
            except Exception:
                pass

            progress.update(task, advance=1)


if __name__ == "__main__":
    asyncio.run(exposure_list_to_lvmcore())
